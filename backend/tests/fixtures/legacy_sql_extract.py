'''
    Author(s):      P10000
    Created on:     2025-11-02
    JIRA/SN #:      TODO: JIRA# or Service Now#
    Description:    Builds the active members table and sends the extract to the FTP bucket.

    Updates:        [2026-02-10, P10000]: pipe-delimited extract
'''

# Base imports   TODO: Developer import packages as needed
import argparse
import logging
import os
import sys
import time
import json
from datetime import datetime, date, timedelta
import pandas as pd
import jaydebeapi
import smtplib
from email.message import EmailMessage

import google
from google.cloud import bigquery
from google.cloud import storage

# Module paths -do not alter unless you know what you are doing!
sys.path.insert(0, '/data/py_scripts')
sys.path.insert(0, '/data/data_eng/py_scripts')

from modules import read_config_yaml as config

credentials, project_id = google.auth.default()
bq_client = bigquery.Client(project=project_id, credentials=credentials)  # Instantiate a BQ Client
storage_client = storage.Client(project=project_id, credentials=credentials)

# Configuration constants
CONFIG = {
    "LOG_FORMAT": "%(asctime)s - %(levelname)s - %(message)s",
    "LOG_LEVEL": logging.INFO,
    "SCRIPT_NAME": os.path.basename(__file__),
    "EXEC_DATETIME": datetime.now()
}

# Set up logging
logging.basicConfig(format=CONFIG["LOG_FORMAT"], level=CONFIG["LOG_LEVEL"])
logger = logging.getLogger(CONFIG["SCRIPT_NAME"])


def build_active_members(write_project, data_sets):
    """Rebuild the active members table from the raw member records"""
    logger.info(f'Function {build_active_members.__name__} execution begin')
    start = time.perf_counter()
    membership_dataset = data_sets.get('membership_dataset')
    try:
        sql = f"""
            create or replace table `{write_project}.{membership_dataset}.ms_acq_dm_active_members` as
            select member_id, join_date
            from `acme-raw-dev.membership.members`
            where status = 'A'
              and join_date >= @cutoff
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter('cutoff', 'DATE', date.today() - timedelta(days=365))]
        )
        response = bq_client.query(sql, job_config=job_config)
        response.result()
        execution_time = round(time.perf_counter() - start, 2)
        logger.info(f"Function {build_active_members.__name__} execution end:  {execution_time} seconds.")
    except Exception as e:
        s = str(e)
        raise RuntimeError(s)


#### Uploading files to gcs buckets using extract job config####
def extract_data_file_csv(config_data):
    write_project = config_data.get('write_project')
    data_sets = config_data.get('data_sets', {})
    membership_dataset = data_sets.get('membership_dataset')
    ftp = config_data.get('ftp', {})
    gcs_write_bucket = ftp.get('bucket', {})
    gcs_write_path = ftp.get('path', {})
    extract_file_name = ftp.get('file_name', {})

    today_date = date.today()
    file_date = today_date.strftime('%Y%m%d')
    extract_file_name = f"{extract_file_name}{file_date}.txt"
    destination_uri = f"gs://{gcs_write_bucket}/{gcs_write_path}{extract_file_name}"
    source_table_id = f'{write_project}.{membership_dataset}.ms_acq_dm_active_members'

    job_config = bigquery.ExtractJobConfig(
        field_delimiter='|',
        print_header=True,
        destination_format=bigquery.DestinationFormat.CSV,
    )
    try:
        extract_job = bq_client.extract_table(source_table_id, destination_uri, location='US', job_config=job_config)
        extract_job.result()
        print("extracted", destination_uri)
    except Exception as e:
        logger.exception("Error occured while extracting the file")
        sys.exit(1)


# Email module
def stream_csv(gcs_write_bucket, gcs_write_path, extract_file_name):
    logger.info("GCS File Download Begin")
    try:
        bucket = storage_client.get_bucket(gcs_write_bucket)
        blob = bucket.blob(gcs_write_path + extract_file_name)
        return blob.download_as_bytes(), extract_file_name
    except Exception as e:
        logger.exception("Error occured while downloading file from GCS Bucket")
        sys.exit(1)


def send_email(sender_email, receiver_email, subject, body, attachment_data, attachment_filename):
    try:
        message = EmailMessage()
        message["From"] = sender_email
        message["To"] = ", ".join(receiver_email)
        message["Subject"] = subject
        message.set_content(body)
        message.add_attachment(attachment_data, maintype='application', subtype='octet-stream', filename=attachment_filename)
        with smtplib.SMTP("smtp.example.com", 25) as server:
            server.starttls()
            server.send_message(message)
    except Exception as e:
        logger.exception("Error occured while attaching file to the email body")
        sys.exit(1)


def email(config_data):
    email = config_data.get('email', {})
    sender_email = email.get('sender')
    receiver_email = email.get('receiver')
    ftp = config_data.get('ftp', {})
    gcs_write_bucket = ftp.get('bucket', {})
    gcs_write_path = ftp.get('path', {})
    extract_file_name = ftp.get('file_name', {})
    file_date = date.today().strftime('%Y%m%d')
    attachment_data, attachment_filename = stream_csv(gcs_write_bucket, gcs_write_path, f"{extract_file_name}{file_date}.txt")
    send_email(sender_email, receiver_email, "Active members extract", "Hello Team, the extract is attached.",
               attachment_data, attachment_filename)


def main(args):
    logger.info(f'Function {main.__name__} execution begin')
    environment = args.environment
    config_file = args.cf
    config_data = config.read_configuration(environment, config_file)
    write_project = config_data.get('write_project')
    data_sets = config_data.get('data_sets', {})

    build_active_members(write_project, data_sets)
    extract_data_file_csv(config_data)
    email(config_data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Retrieve process configuration.')
    parser.add_argument('environment', choices=['lab', 'value'], help='Environment (lab, value)')
    parser.add_argument('-cf', default='dl_active_members.yaml', help="Configuration filename and path")
    args = parser.parse_args()
    main(args)
