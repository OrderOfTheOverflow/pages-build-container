'''
Classes and methods for publishing a directory to S3
'''

import glob
import requests

from os import path, makedirs

from log_utils import get_logger
from .models import (remove_prefix, SiteObject, SiteFile, SiteRedirect)

MAX_S3_KEYS_PER_REQUEST = 1000
FEDERALIST_JSON = 'federalist.json'


def list_remote_objects(bucket, site_prefix, s3_client):
    '''

    Generates a list of remote S3 objects that have keys starting with
    site_preix in the given bucket.

    '''
    results_truncated = True
    continuation_token = None

    remote_objects = []

    while results_truncated:
        prefix = site_prefix
        # Add a / to the end of the prefix to prevent
        # retrieving keys for sites with site_prefixes
        # that are substrings of others
        if prefix[-1] != '/':
            prefix += '/'

        request_kwargs = {
            'Bucket': bucket,
            'MaxKeys': MAX_S3_KEYS_PER_REQUEST,
            'Prefix': prefix,
        }

        if continuation_token:
            request_kwargs['ContinuationToken'] = continuation_token

        response = s3_client.list_objects_v2(**request_kwargs)

        contents = response.get('Contents')
        if not contents:
            return remote_objects

        for response_obj in contents:
            # remove the site_prefix from the key
            filename = remove_prefix(response_obj['Key'], site_prefix)

            # remove initial slash if present
            filename = remove_prefix(filename, '/')

            # the etag comes surrounded by double quotes, so remove them
            md5 = response_obj['ETag'].replace('"', '')

            site_obj = SiteObject(filename=filename, md5=md5,
                                  site_prefix=site_prefix)
            remote_objects.append(site_obj)

        results_truncated = response['IsTruncated']
        if results_truncated:
            continuation_token = response['NextContinuationToken']

    return remote_objects


def path_is_excluded(federalist_config, filename, dir_prefix):
    filepath = filename
    if dir_prefix and filepath.startswith(dir_prefix):
        filepath = filepath[len(dir_prefix):]

    return federalist_config.is_path_excluded(filepath)


def get_cache_control(federalist_config, filename, dir_prefix):
    filepath = filename
    if dir_prefix and filepath.startswith(dir_prefix):
        filepath = filepath[len(dir_prefix):]

    return federalist_config.get_headers_for_path(filepath).get('cache-control')


def publish_to_s3(directory, base_url, site_prefix, bucket, federalist_config,
                  s3_client, dry_run=False):
    '''Publishes the given directory to S3'''
    logger = get_logger('publish')

    # With glob, dotfiles are ignored by default
    # Note that the filenames will include the `directory` prefix
    # but we won't want that for the eventual S3 keys
    files_and_dirs = glob.glob(path.join(directory, '**', '*'),
                               recursive=True)

    # add security.txt support
    files_and_dirs += glob.glob(path.join(directory, '**', '.well-known',
                                'security.txt'), recursive=True)
    # Collect a list of all files in the specified directory
    local_files = []
    for filename in files_and_dirs:
        if path.isfile(filename):
            cache_control = get_cache_control(federalist_config, filename, directory)

            site_file = SiteFile(filename=filename,
                                 dir_prefix=directory,
                                 site_prefix=site_prefix,
                                 cache_control=cache_control)
            local_files.append(site_file)

    # Add local 404 if does not already exist
    filename_404 = directory + '/404.html'
    default_404_url = ('https://raw.githubusercontent.com'
                       '/18F/federalist-404-page/master/'
                       '404-federalist-client.html')
    if not path.isfile(filename_404):
        default_404 = requests.get(default_404_url)
        makedirs(path.dirname(filename_404), exist_ok=True)
        with open(filename_404, "w+") as f:
            f.write(default_404.text)

        cache_control = get_cache_control(federalist_config, filename_404, directory)

        file_404 = SiteFile(filename=filename_404,
                            dir_prefix=directory,
                            site_prefix=site_prefix,
                            cache_control=cache_control)
        local_files.append(file_404)

    # Exclude files based on defaults and configuration
    local_files[:] = [file for file in local_files
                      if not path_is_excluded(federalist_config, file.filename, directory)]

    # Create a list of redirects from the local files
    local_redirects = []
    for site_file in local_files:
        if path.basename(site_file.filename) == 'index.html':
            redirect_filename = path.dirname(site_file.filename)
            site_redirect = SiteRedirect(filename=redirect_filename,
                                         dir_prefix=directory,
                                         site_prefix=site_prefix,
                                         base_url=base_url)
            local_redirects.append(site_redirect)

    # Combined list of local objects
    local_objects = local_files + local_redirects

    if len(local_objects) == 0:
        raise RuntimeError('Local build files not found')

    # Get list of remote files
    remote_objects = list_remote_objects(bucket=bucket,
                                         site_prefix=site_prefix,
                                         s3_client=s3_client)

    # Make dicts by filename of local and remote objects for easier searching
    remote_objects_by_filename = {}
    for obj in remote_objects:
        # These will not have the `directory` prefix that our local
        # files do, so add it so we can more easily compare them.
        filename = path.join(directory, obj.filename)
        remote_objects_by_filename[filename] = obj

    local_objects_by_filename = {}
    for obj in local_objects:
        local_objects_by_filename[obj.filename] = obj

    # Create lists of all the new and modified objects
    new_objects = []
    replacement_objects = []
    for local_filename, local_obj in local_objects_by_filename.items():
        matching_remote_obj = remote_objects_by_filename.get(local_filename)
        if not matching_remote_obj:
            new_objects.append(local_obj)
        else:
            replacement_objects.append(local_obj)

    # Create a list of the remote objects that should be deleted
    deletion_objects = [
        obj for filename, obj in remote_objects_by_filename.items()
        if not local_objects_by_filename.get(filename)
    ]

    if (len(new_objects) == 0 and len(replacement_objects) <= 1 and
            len(local_files) <= 1):
        raise RuntimeError('Cannot unpublish all files')

    logger.info('Preparing to upload')
    logger.info(f'New: {len(new_objects)}')
    logger.info(f'Replaced: {len(replacement_objects)}')
    logger.info(f'Deleted: {len(deletion_objects)}')

    # Upload new and replacement files
    upload_objects = new_objects + replacement_objects
    for file in upload_objects:
        if dry_run:  # pragma: no cover
            logger.info(f'Dry-run uploading {file.s3_key}')
        else:
            logger.info(f'Uploading {file.s3_key}')

            try:
                file.upload_to_s3(bucket, s3_client)
            except UnicodeEncodeError as err:
                if err.reason == 'surrogates not allowed':
                    logger.warning(
                        f'... unable to upload {file.filename} due '
                        f'to invalid characters in file name.'
                    )
                else:
                    raise

    # Delete files not needed any more
    for file in deletion_objects:
        if dry_run:  # pragma: no cover
            logger.info(f'Dry run deleting {file.s3_key}')
        else:
            logger.info(f'Deleting {file.s3_key}')

            file.delete_from_s3(bucket, s3_client)
