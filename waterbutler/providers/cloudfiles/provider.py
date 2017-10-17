import os
import hmac
import json
import time
import asyncio
import hashlib
import functools

import furl

from waterbutler.core import streams
from waterbutler.core import provider
from waterbutler.core import exceptions
from waterbutler.core.path import WaterButlerPath

from waterbutler.providers.cloudfiles import settings
from waterbutler.providers.cloudfiles.metadata import (
    CloudFilesFileMetadata,
    CloudFilesFolderMetadata,
    CloudFilesHeaderMetadata,
    CloudFilesRevisonMetadata
)


def ensure_connection(func):
    """Runs ``_ensure_connection`` before continuing to the method
    """
    @functools.wraps(func)
    async def wrapped(self, *args, **kwargs):
        await self._ensure_connection()
        return (await func(self, *args, **kwargs))
    return wrapped


class CloudFilesProvider(provider.BaseProvider):
    """Provider for Rackspace CloudFiles.

    API Docs: https://developer.rackspace.com/docs/cloud-files/v1/developer-guide/
    #document-developer-guide
    """
    NAME = 'cloudfiles'

    def __init__(self, auth, credentials, settings):
        super().__init__(auth, credentials, settings)
        self.token = None
        self.endpoint = None
        self.public_endpoint = None
        self.temp_url_key = credentials.get('temp_key', '').encode()
        self.region = self.credentials['region']
        self.og_token = self.credentials['token']
        self.username = self.credentials['username']
        self.container = self.settings['container']
        self.use_public = self.settings.get('use_public', True)
        self.metrics.add('region', self.region)

    async def validate_v1_path(self, path, **kwargs):
        return await self.validate_path(path, **kwargs)

    async def validate_path(self, path, **kwargs):
        return WaterButlerPath(path)

    @property
    def default_headers(self):
        return {
            'X-Auth-Token': self.token,
            'Accept': 'application/json',
        }

    @ensure_connection
    async def intra_copy(self, dest_provider, source_path, dest_path):
        exists = await dest_provider.exists(dest_path)

        resp = await self.make_request(
            'PUT',
            functools.partial(dest_provider.build_url, dest_path.path),
            headers={
                'X-Copy-From': os.path.join(self.container, source_path.path)
            },
            expects=(201, ),
            throws=exceptions.IntraCopyError,
        )
        await resp.release()
        return (await dest_provider.metadata(dest_path)), not exists

    @ensure_connection
    async def download(self,
                       path,
                       accept_url=False,
                       range=None,
                       version=None,
                       revision=None,
                       displayName=None,
                       **kwargs):
        """Returns a ResponseStreamReader (Stream) for the specified path
        :param str path: Path to the object you want to download
        :param dict \*\*kwargs: Additional arguments that are ignored
        :rtype str:
        :rtype ResponseStreamReader:
        :raises: exceptions.DownloadError
        """
        self.metrics.add('download.accept_url', accept_url)
        if accept_url:
            parsed_url = furl.furl(self.sign_url(path, endpoint=self.public_endpoint))
            parsed_url.args['filename'] = displayName or path.name
            return parsed_url.url

        version = revision or version
        if version:
            return await self._download_revision(range, version)

        resp = await self.make_request(
            'GET',
            functools.partial(self.sign_url, path),
            range=range,
            expects=(200, 206),
            throws=exceptions.DownloadError,
        )
        return streams.ResponseStreamReader(resp)

    @ensure_connection
    async def upload(self, stream, path, check_created=True, fetch_metadata=True):
        """Uploads the given stream to CloudFiles
        :param ResponseStreamReader stream: The stream to put to CloudFiles
        :param str path: The full path of the object to upload to/into
        :param bool check_created: This checks if uploaded file already exists
        :param bool fetch_metadata: If true upload will return metadata
        :rtype (dict/None, bool):
        """
        if check_created:
            created = not (await self.exists(path))
        else:
            created = None
        self.metrics.add('upload.check_created', check_created)

        stream.add_writer('md5', streams.HashStreamWriter(hashlib.md5))
        resp = await self.make_request(
            'PUT',
            functools.partial(self.sign_url, path, 'PUT'),
            data=stream,
            headers={'Content-Length': str(stream.size)},
            expects=(200, 201),
            throws=exceptions.UploadError,
        )
        await resp.release()
        # md5 is returned as ETag header as long as server side encryption is not used.
        if stream.writers['md5'].hexdigest != resp.headers['ETag'].replace('"', ''):
            raise exceptions.UploadChecksumMismatchError()

        if fetch_metadata:
            metadata = await self.metadata(path)
        else:
            metadata = None
        self.metrics.add('upload.fetch_metadata', fetch_metadata)

        return metadata, created

    @ensure_connection
    async def delete(self, path, confirm_delete=False):
        """Deletes the key at the specified path
        :param str path: The path of the key to delete
        :param bool confirm_delete: not used in this provider
        :rtype None:
        """
        if path.is_dir:
            metadata = await self._metadata_folder(path, recursive=True)

            delete_files = [
                os.path.join('/', self.container, path.child(item.name).path)
                for item in metadata
            ]

            delete_files.append(os.path.join('/', self.container, path.path))
            query = {'bulk-delete': ''}
            resp = await self.make_request(
                'DELETE',
                functools.partial(self.build_url, '', **query),
                data='\n'.join(delete_files),
                expects=(200, ),
                throws=exceptions.DeleteError,
                headers={
                    'Content-Type': 'text/plain',
                },
            )
            await resp.release()

        resp = await self.make_request(
            'DELETE',
            functools.partial(self.build_url, path.path),
            expects=(204, ),
            throws=exceptions.DeleteError,
        )
        await resp.release()

    @ensure_connection
    async def metadata(self, path, recursive=False, version=None, revision=None, **kwargs):
        """Get Metadata about the requested file or folder
        :param str path: The path to a key or folder
        :rtype dict:
        :rtype list:
        """
        if path.is_dir:
            return (await self._metadata_folder(path, recursive=recursive))
        elif version or revision:
            return (await self.get_metadata_revision(path, version, revision))
        else:
            return (await self._metadata_item(path))

    def build_url(self, path, _endpoint=None, container=None, **query):
        """Build the url for the specified object
        :param args segments: URI segments
        :param kwargs query: Query parameters
        :rtype str:
        """
        endpoint = _endpoint or self.endpoint
        container = container or self.container
        return provider.build_url(endpoint, container, *path.split('/'), **query)

    def can_duplicate_names(self):
        return False

    def can_intra_copy(self, dest_provider, path=None):
        return type(self) == type(dest_provider) and not getattr(path, 'is_dir', False)

    def can_intra_move(self, dest_provider, path=None):
        return type(self) == type(dest_provider) and not getattr(path, 'is_dir', False)

    def sign_url(self, path, method='GET', endpoint=None, seconds=settings.TEMP_URL_SECS):
        """Sign a temp url for the specified stream
        :param str stream: The requested stream's path
        :param CloudFilesPath path: A path to a file/folder
        :param str method: The HTTP method used to access the returned url
        :param int seconds: Time for the url to live
        :rtype str:
        """
        from urllib.parse import unquote

        method = method.upper()
        expires = str(int(time.time() + seconds))
        path_str = path.path
        url = furl.furl(self.build_url(path_str, _endpoint=endpoint))

        body = '\n'.join([method, expires])
        body += '\n' + str(url.path)
        body = unquote(body).encode()
        signature = hmac.new(self.temp_url_key, body, hashlib.sha1).hexdigest()
        url.args.update({
            'temp_url_sig': signature,
            'temp_url_expires': expires,
        })

        return unquote(str(url.url))

    async def make_request(self, *args, **kwargs):
        try:
            return (await super().make_request(*args, **kwargs))
        except exceptions.ProviderError as e:
            if e.code != 408:
                raise
            await asyncio.sleep(1)
            return (await super().make_request(*args, **kwargs))

    async def _ensure_connection(self):
        """Defines token, endpoint and temp_url_key if they are not already defined
        :raises ProviderError: If no temp url key is available
        """
        # Must have a temp url key for download and upload
        # Currently You must have one for everything however
        self.metrics.add('ensure_connection.has_token_and_endpoint', True)
        self.metrics.add('ensure_connection.has_temp_url_key', True)
        if not self.token or not self.endpoint:
            self.metrics.add('ensure_connection.has_token_and_endpoint', False)
            data = await self._get_token()
            self.token = data['access']['token']['id']
            self.metrics.add('ensure_connection.use_public', True if self.use_public else False)
            if self.use_public:
                self.public_endpoint, _ = self._extract_endpoints(data)
                self.endpoint = self.public_endpoint
            else:
                self.public_endpoint, self.endpoint = self._extract_endpoints(data)
        if not self.temp_url_key:
            self.metrics.add('ensure_connection.has_temp_url_key', False)
            async with self.request('HEAD', self.endpoint, expects=(204, )) as resp:
                try:
                    self.temp_url_key = resp.headers['X-Account-Meta-Temp-URL-Key'].encode()
                except KeyError:
                    raise exceptions.ProviderError('No temp url key is available', code=503)

    def _extract_endpoints(self, data):
        """Pulls both the public and internal cloudfiles urls,
        returned respectively, from the return of tokens
        Very optimized.
        :param dict data: The json response from the token endpoint
        :rtype (str, str):
        """
        for service in reversed(data['access']['serviceCatalog']):
            if service['name'].lower() == 'cloudfiles':
                for region in service['endpoints']:
                    if region['region'].lower() == self.region.lower():
                        return region['publicURL'], region['internalURL']

    async def _get_token(self):
        """Fetches an access token from cloudfiles for actual api requests
        Returns the entire json response from the tokens endpoint
        Notably containing our token and proper endpoint to send requests to
        :rtype dict:
        """
        resp = await self.make_request(
            'POST',
            settings.AUTH_URL,
            data=json.dumps({
                'auth': {
                    'RAX-KSKEY:apiKeyCredentials': {
                        'username': self.username,
                        'apiKey': self.og_token,
                    }
                }
            }),
            headers={
                'Content-Type': 'application/json',
            },
            expects=(200, ),
        )
        data = await resp.json()
        return data

    async def _metadata_item(self, path):
        """Get Metadata about the requested file
        :param str path: The path to a key
        :rtype dict:
        :rtype list:
        """
        resp = await self.make_request(
            'HEAD',
            functools.partial(self.build_url, path.path),
            expects=(200, ),
            throws=exceptions.MetadataError,
        )
        await resp.release()

        if resp.headers['Content-Type'] == 'application/directory' and path.is_file:
            raise exceptions.MetadataError(
                'Could not retrieve file \'{0}\''.format(str(path)),
                code=404,
            )

        return CloudFilesHeaderMetadata(dict(resp.headers), path)

    async def _metadata_folder(self, path, recursive=False):
        """Get Metadata about the requested folder
        :param str path: The path to a folder
        :rtype dict:
        :rtype list:
        """
        # prefix must be blank when searching the root of the container
        query = {'prefix': path.path}
        self.metrics.add('metadata.folder.is_recursive', True if recursive else False)
        if not recursive:
            query.update({'delimiter': '/'})
        resp = await self.make_request(
            'GET',
            functools.partial(self.build_url, '', **query),
            expects=(200, ),
            throws=exceptions.MetadataError,
        )
        data = await resp.json()

        # no data and the provider path is not root, we are left with either a file or a directory
        # marker
        if not data and not path.is_root:
            # Convert the parent path into a directory marker (file) and check for an empty folder
            dir_marker = path.parent.child(path.name, folder=path.is_dir)
            metadata = await self._metadata_item(dir_marker)
            if not metadata:
                raise exceptions.MetadataError(
                    'Could not retrieve folder \'{0}\''.format(str(path)),
                    code=404,
                )

        # normalized metadata, remove extraneous directory markers
        for item in data:
            if 'subdir' in item:
                for marker in data:
                    if 'content_type' in marker and marker['content_type'] == 'application/directory':
                        subdir_path = item['subdir'].rstrip('/')
                        if marker['name'] == subdir_path:
                            data.remove(marker)
                            break

        return [
            self._serialize_metadata(item)
            for item in data
        ]

    def _serialize_metadata(self, data):
        if data.get('subdir'):
            return CloudFilesFolderMetadata(data)
        elif data['content_type'] == 'application/directory':
            return CloudFilesFolderMetadata({'subdir': data['name'] + '/'})
        return CloudFilesFileMetadata(data)

    @ensure_connection
    async def create_folder(self, path, folder_precheck=None):

        resp = await self.make_request(
            'PUT',
            functools.partial(self.sign_url, path, 'PUT'),
            expects=(200, 201),
            throws=exceptions.UploadError,
            headers={'Content-Type': 'application/directory'}
        )
        await resp.release()
        return await self._metadata_item(path)

    @ensure_connection
    async def _get_version_location(self):
        resp = await self.make_request(
            'HEAD',
            functools.partial(self.build_url, ''),
            expects=(200, 204),
            throws=exceptions.MetadataError,
        )
        await resp.release()

        try:
            return resp.headers['X-VERSIONS-LOCATION']
        except KeyError:
            raise exceptions.MetadataError('The your container does not have a defined version'
                                           ' location. To set a version location and store file '
                                           'versions follow the instructions here: '
                                           'https://developer.rackspace.com/docs/cloud-files/v1/'
                                           'use-cases/additional-object-services-information/'
                                           '#object-versioning')

    @ensure_connection
    async def revisions(self, path):
        version_location = await self._get_version_location()

        query = {'prefix': '{:03x}'.format(len(path.name)) + path.name + '/'}
        resp = await self.make_request(
            'GET',
            functools.partial(self.build_url, '', container=version_location, **query),
            expects=(200, 204),
            throws=exceptions.MetadataError,
        )
        json_resp = await resp.json()
        current = (await self.metadata(path)).to_revision()
        return [current] + [CloudFilesRevisonMetadata(revision_data) for revision_data in json_resp]

    @ensure_connection
    async def get_metadata_revision(self, path, version=None, revision=None):
        version_location = await self._get_version_location()

        resp = await self.make_request(
            'HEAD',
            functools.partial(self.build_url, version or revision, container=version_location),
            expects=(200, ),
            throws=exceptions.MetadataError,
        )
        await resp.release()

        return CloudFilesHeaderMetadata(resp.headers, path)

    async def _download_revision(self, range, version):

        version_location = await self._get_version_location()

        resp = await self.make_request(
            'GET',
            functools.partial(self.build_url, version, container=version_location),
            range=range,
            expects=(200, 206),
            throws=exceptions.DownloadError
        )

        return streams.ResponseStreamReader(resp)
