"""
    gsm_module — read and write Google Secret Manager secrets (JSON payloads), e.g. source-system connections.

    Usage:
        secret = gsm_module.google_secret(credentials, gsm_project)
        secret.set_secret_name("my-secret")
        secret.set_version("latest")
        response = secret.get_gsm_secret()
        payload = json.loads(response.payload.data.decode("UTF-8"))

    Hub copy of /data/data_eng/py_scripts/modules/gsm_module.py (rebuilt from screenshots; keep identical).
"""

from google.cloud import secretmanager
import csv
import pprint
import json
import sys  # noqa: F401  (kept as in the original module)

class google_secret:

    def __init__(self, *args):

        self._l_credentials = args[0]
        self._project_id = args[1]
        self._client = secretmanager.SecretManagerServiceClient(credentials=self._l_credentials)
        self._project_detail = f"projects/{self._project_id}"

        # These values must be set by using the setter functions
        self._secret_name = None
        self._version = None
        self._json_data = None
        self._file = None
        self._file_type = None

    # Setters
    def set_secret_name(self, name):
        self._secret_name = name

    def set_version(self, version):
        self._version = version

    def set_file(self, file):
        self._file = file

    def set_file_type(self, file_type):
        self._file_type = file_type

    def set_payload(self, json):
        self._json_data = json

    # Getters
    def get_secret_name(self):
        return self._secret_name

    def get_credentials(self):
        return self._l_credentials

    def get_project_id(self):
        return self._project_id

    # Get and return raw payload data
    def get_gsm_secret(self):

        try:
            name = f"projects/{self._project_id}/secrets/{self._secret_name}/versions/{self._version}"      # Build the resource name of the secret version
            response = self._client.access_secret_version(request={"name": name})   # Access the secret version for Service Account Connection

            return response

        except Exception as e:
            s = str(e)
            print("ERROR: " + s)
            raise RuntimeError(e)

    # Display payload
    def display_gsm_secret(self):

        try:
            print (f'Displaying Google Secret payload for: {self._secret_name}')

            response = google_secret.get_gsm_secret(self)
            payload = response.payload.data.decode("UTF-8")
            json_object = json.loads(payload)

            print (f"GSM Key: {str(response.name[:response.name.find('versions')+9])+ self._version}")
            print ("\nPayload:")
            pprint.pprint(json_object)

        except Exception as e:
            s = str(e)
            print("ERROR: " + s)
            raise RuntimeError(e)

    # Write payload
    def write_gsm_secret(self):

        try:
            response = google_secret.get_gsm_secret(self)
            payload = response.payload.data.decode("UTF-8")
            json_object = json.loads(payload)

            print (f"Writing data to file: {self._file}")

            if self._file_type == 'csv':
                file_headers = ['id', 'host', 'port', 'database', 'schema', 'userid', 'password']
                with open(self._file, "w",newline='') as gsm_file:
                    writer = csv.DictWriter(gsm_file, fieldnames = file_headers)
                    writer.writeheader()
                    for data in json_object['database_credentials']:
                        writer.writerow(data)

                gsm_file.close()

            if self._file_type == 'json':
                with open(self._file, "w",newline='') as gsm_file:
                    gsm_file.write(payload)
                    gsm_file.close()

        except Exception as e:
            s = str(e)
            print("ERROR: " + s)
            raise RuntimeError(e)

    # Insert value
    def insert_gsm_secret(self):

        print (f'Inserting Google Secret Value for: {self._secret_name}')
        if not google_secret.secret_exists(self): #secret_exists:
            try:
                # create the secret
                print (f"Creating secret key: {self._secret_name}")
                response = self._client.create_secret(
                    request={
                        "parent": self._project_detail,
                        "secret_id": self._secret_name,
                        "secret": {"replication": {"user_managed": {"replicas": [{'location': 'us-central1'}]}}},
                    }
                )

            except Exception as e:
                s = str(e)
                print("ERROR: " + s)
                raise RuntimeError(e)
        else:
            print (f"Secret key: {self._secret_name} exists")

        # Insert new version
        print (f"Creating version in secret key: {self._secret_name}")
        parent = self._client.secret_path(self._project_id, self._secret_name)
        payload = str(self._json_data).replace("'","\"").encode("UTF-8")

        response = self._client.add_secret_version(
            request={
                "parent": parent,
                "payload": {"data": payload},
            }
        )

        print (f"Added version: {response.name}")

    # Method to check if secret exists
    def secret_exists(self):

        secret_exists = False

        try:
            secrets = self._client.list_secrets(request={"parent": self._project_detail})

            # Check if exists, if so then insert new version
            for secret in secrets:
                if secret.name.split('/')[3] == self._secret_name:
                    secret_exists = True

            return secret_exists

        except Exception as e:
            s = str(e)
            print("ERROR: " + s)
            raise RuntimeError(e)

    def get_all_secrets(self):

        secrets = self._client.list_secrets(request={"parent": self._project_detail})

        return secrets
