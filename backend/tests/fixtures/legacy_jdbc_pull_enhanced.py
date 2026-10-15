'''
    Author(s):      P12345
    Reviewer(s):    TODO: reviewer PID(s)
    Created on:     2024-05-01
    JIRA/SN #:      DE-77

    Description:    Pulls the member table from the PDW source system and loads it into BigQuery.

    Usage:          py ./pdw_pull.py -cf ./pdw_pull.yaml lab

    Updates:        [YYYY-MM-DD, PID]:
'''

# Base imports
import argparse
import logging
import os
import sys
import time
from datetime import datetime

import google.auth
import jaydebeapi
import pandas as pd
from google.cloud import bigquery

# Module paths -do not alter unless you know what you are doing!
sys.path.insert(0, '/data/py_scripts')
sys.path.insert(0, '/data/data_eng/py_scripts')

from modules import read_config_yaml as config

credentials, project_id = google.auth.default()
bq_client = bigquery.Client(project=project_id, credentials=credentials)

CONFIG = {
    "LOG_FORMAT": "%(asctime)s - %(levelname)s - %(message)s",
    "LOG_LEVEL": logging.INFO,
    "SCRIPT_NAME": os.path.basename(__file__),
    "EXEC_DATETIME": datetime.now(),
}
logging.basicConfig(format=CONFIG["LOG_FORMAT"], level=CONFIG["LOG_LEVEL"])
logger = logging.getLogger(CONFIG["SCRIPT_NAME"])


def pull_members(conn_info):
    """Read the member table from PDW over JDBC"""
    logger.info(f'Function {pull_members.__name__} execution begin')
    start = time.perf_counter()
    try:
        conn = jaydebeapi.connect(conn_info["driver"], conn_info["url"], [conn_info["user"], conn_info["password"]], conn_info["jar"])
        df = pd.read_sql("select * from pdw.members", conn)
        conn.close()
        execution_time = round(time.perf_counter() - start, 2)
        logger.info(f"Function {pull_members.__name__} execution end:  {execution_time} seconds.")
        return df
    except Exception as e:
        s = str(e)
        raise RuntimeError(s)


def load_members(df, write_project):
    """Load the members into the staging table"""
    logger.info(f'Function {load_members.__name__} execution begin')
    start = time.perf_counter()
    try:
        bq_client.load_table_from_dataframe(df, f"{write_project}.staging.members").result()
        sql = f"""
        insert into `{write_project}.membership.members`
        select * from `{write_project}.staging.members`
    """
        bq_client.query(sql).result()
        execution_time = round(time.perf_counter() - start, 2)
        logger.info(f"Function {load_members.__name__} execution end:  {execution_time} seconds.")
    except Exception as e:
        s = str(e)
        raise RuntimeError(s)


def main(args):
    config_data = config.read_configuration(args.environment, args.cf)
    conn_info = config_data.get("pdw")
    df = pull_members(conn_info)
    load_members(df, config_data.get("write_project"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retrieve process configuration.")
    parser.add_argument('environment', choices=['lab', 'value', 'qa'], help="Environment")
    parser.add_argument('-cf', default='pdw_pull.yaml', help="Configuration filename and path")
    args = parser.parse_args()
    main(args)
