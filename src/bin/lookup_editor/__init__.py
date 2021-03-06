"""
This module includes the main functions for editing lookup files. This class serves as an entry
point to the rest of the related lookup editing modules. The REST handler and/or controller
shouldn't need to call functions in the dependencies.
"""

import os
import codecs
import json

import splunk
from splunk import AuthorizationFailed, ResourceNotFound
from splunk.appserver.mrsparkle.lib.util import make_splunkhome_path

from lookup_editor.lookup_backups import LookupBackups
from lookup_editor.exceptions import LookupFileTooBigException, PermissionDeniedException
from lookup_editor.shortcuts import flatten_dict
from lookup_editor import lookupfiles
from lookup_editor import settings

class LookupEditor(LookupBackups):
    """
    This class provides functions for editing lookup files. It is bundled in an instantiable class
    so that it can be given a logger.

    This class inherits from LookupBackups in order to be able to leverage the .
    """

    def __init__(self, logger):
        super(LookupEditor, self).__init__(logger)

    def get_kv_lookup(self, session_key, lookup_file, namespace="lookup_editor", owner=None):
        """
        Get the contents of a KV store lookup.
        """

        if owner is None:
            owner = 'nobody'

        lookup_contents = []

        # Get the fields so that we can compose the header
        # Note: this call must be done with the user context of "nobody".
        response, content = splunk.rest.simpleRequest('/servicesNS/nobody/' + namespace +
                                                      '/storage/collections/config/' +
                                                      lookup_file,
                                                      sessionKey=session_key,
                                                      getargs={'output_mode': 'json'})

        if response.status == 403:
            raise PermissionDeniedException("You do not have permission to view this lookup")

        header = json.loads(content)

        fields = ['_key']

        for field in header['entry'][0]['content']:
            if field.startswith('field.'):
                fields.append(field[6:])

        lookup_contents.append(fields)

        # Get the contents
        response, content = splunk.rest.simpleRequest('/servicesNS/' + owner + '/' + namespace +
                                                      '/storage/collections/data/' + lookup_file,
                                                      sessionKey=session_key,
                                                      getargs={'output_mode': 'json'})

        if response.status == 403:
            raise PermissionDeniedException("You do not have permission to view this lookup")

        rows = json.loads(content)

        for row in rows:
            new_row = []

            # Convert the JSON style format of the row and convert it down to chunk of text
            flattened_row = flatten_dict(row, fields=fields)

            # Add each field to the table row
            for field in fields:

                # If the field was found, add it
                if field in flattened_row:
                    new_row.append(flattened_row[field])

                # If the field wasn't found, add a blank string. We need to do this to make
                # sure that the number of columns is consistent. We can't have fewer data
                # columns than we do header columns. Otherwise, the header won't line up with
                # the field since the number of columns items in the header won't match the
                # number of columns in the rows.
                else:
                    new_row.append("")

            lookup_contents.append(new_row)

        return lookup_contents

    def get_lookup(self, session_key, lookup_file, namespace="lookup_editor", owner=None,
                   get_default_csv=True, version=None, throw_exception_if_too_big=False):
        """
        Get a file handle to the associated lookup file.
        """

        self.logger.debug("Version is:" + str(version))

        # Check capabilities
        #LookupEditor.check_capabilities(lookup_file, user, session_key)

        # Get the file path
        file_path = self.resolve_lookup_filename(lookup_file, namespace, owner, get_default_csv,
                                                 version, session_key=session_key)

        if throw_exception_if_too_big:

            try:
                file_size = os.path.getsize(file_path)

                self.logger.info('Size of lookup file determined, file_size=%s, path=%s',
                                 file_size, file_path)

                if file_size > settings.MAXIMUM_EDITABLE_SIZE:
                    raise LookupFileTooBigException(file_size)

            except os.error:
                self.logger.exception("Exception generated when attempting to determine size of " +
                                      "requested lookup file")

        self.logger.info("Loading lookup file from path=%s", file_path)

        # Get the file handle
        # Note that we are assuming that the file is in UTF-8. Any characters that don't match
        # will be replaced.
        return codecs.open(file_path, 'rb', encoding='utf-8', errors='replace')


    def resolve_lookup_filename(self, lookup_file, namespace="lookup_editor", owner=None,
                                get_default_csv=True, version=None, throw_not_found=True,
                                session_key=None):
        """
        Resolve the lookup filename. This function will handle things such as:
         * Returning the default lookup file if requested
         * Returning the path to a particular version of a file

        Note that the lookup file must have an existing lookup file entry for this to return
        correctly; this shouldn't be used for determining the path of a new file.
        """

        # Strip out invalid characters like ".." so that this cannot be used to conduct an
        # directory traversal
        lookup_file = os.path.basename(lookup_file)
        namespace = os.path.basename(namespace)

        if owner is not None:
            owner = os.path.basename(owner)

        # Determine the lookup path by asking Splunk
        try:
            resolved_lookup_path = lookupfiles.SplunkLookupTableFile.get(lookupfiles.SplunkLookupTableFile.build_id(lookup_file, namespace, owner), sessionKey=session_key).path
        except ResourceNotFound:
            if throw_not_found:
                raise
            else:
                return None

        # Get the backup file for one without an owner
        if version is not None and owner is not None:
            lookup_path = make_splunkhome_path([self.get_backup_directory(lookup_file, namespace, owner, resolved_lookup_path=resolved_lookup_path), version])
            lookup_path_default = make_splunkhome_path(["etc", "users", owner, namespace,
                                                        "lookups", lookup_file + ".default"])

        # Get the backup file for one with an owner
        elif version is not None:
            lookup_path = make_splunkhome_path([self.get_backup_directory(lookup_file, namespace, owner, resolved_lookup_path=resolved_lookup_path), version])
            lookup_path_default = make_splunkhome_path(["etc", "apps", namespace, "lookups",
                                                        lookup_file + ".default"])

        # Get the user lookup
        elif owner is not None and owner != 'nobody':
            # e.g. $SPLUNK_HOME/etc/users/luke/SA-NetworkProtection/lookups/test.csv
            lookup_path = resolved_lookup_path
            lookup_path_default = make_splunkhome_path(["etc", "users", owner, namespace,
                                                        "lookups", lookup_file + ".default"])

        # Get the non-user lookup
        else:
            lookup_path = resolved_lookup_path
            lookup_path_default = make_splunkhome_path(["etc", "apps", namespace, "lookups",
                                                        lookup_file + ".default"])

        self.logger.info('Resolved lookup file, path=%s', lookup_path)

        # Get the file path
        if get_default_csv and not os.path.exists(lookup_path) and os.path.exists(lookup_path_default):
            return lookup_path_default
        else:
            return lookup_path

    def is_empty(self, row):
        """
        Determines if the given row in a lookup is empty. This is done in order to prune rows that
        are empty.
        """

        for entry in row:
            if entry is not None and len(entry.strip()) > 0:
                return False

        return True

    def force_lookup_replication(self, app, filename, session_key, base_uri=None):
        """
        Force replication of a lookup table in a Search Head Cluster.
        """

        # Permit override of base URI in order to target a remote server.
        endpoint = '/services/replication/configuration/lookup-update-notify'

        if base_uri:
            repl_uri = base_uri + endpoint
        else:
            repl_uri = endpoint

        # Provide the data that describes the lookup
        payload = {
            'app': app,
            'filename': os.path.basename(filename),
            'user': 'nobody'
        }

        # Perform the request
        response, content = splunk.rest.simpleRequest(repl_uri,
                                                      method='POST',
                                                      postargs=payload,
                                                      sessionKey=session_key,
                                                      raiseAllErrors=False)

        # Analyze the response
        if response.status == 400:
            if 'No local ConfRepo registered' in content:
                # search head clustering not enabled
                self.logger.info('Lookup table replication not applicable for %s: clustering not enabled',
                                 filename)

                return (True, response.status, content)

            elif 'Could not find lookup_table_file' in content:
                self.logger.error('Lookup table replication failed for %s: status_code="%s", content="%s"',
                                  filename, response.status, content)

                return (False, response.status, content)

            else:
                # Previously unforeseen 400 error.
                self.logger.error('Lookup table replication failed for %s: status_code="%s", content="%s"',
                                  filename, response.status, content)

                return (False, response.status, content)

        elif response.status != 200:
            return (False, response.status, content)

        # Return a default response
        self.logger.info('Lookup table replication forced for %s', filename)
        return (True, response.status, content)
