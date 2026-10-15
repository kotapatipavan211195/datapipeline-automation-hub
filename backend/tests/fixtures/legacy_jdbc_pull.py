'''
    Author(s):      P12345
    Created on:     2024-05-01
    JIRA/SN #:      DE-77
    Description:    Pulls the member table from the PDW source system and loads it into BigQuery.
    Updates:        [YYYY-MM-DD, PID]:
'''
import logging
import sys

import jaydebeapi
import pandas as pd
from google.cloud import bigquery

bq_client = bigquery.Client()
logger = logging.getLogger("pdw_pull")


def pull_members(conn_info):
    """Read the member table from PDW over JDBC"""
    conn = jaydebeapi.connect(conn_info["driver"], conn_info["url"], [conn_info["user"], conn_info["password"]], conn_info["jar"])
    df = pd.read_sql("select * from pdw.members", conn)
    conn.close()
    return df


def load_members(df, write_project):
    """Load the members into the staging table"""
    bq_client.load_table_from_dataframe(df, f"{write_project}.staging.members").result()
    sql = f"""
        insert into `{write_project}.membership.members`
        select * from `{write_project}.staging.members`
    """
    bq_client.query(sql).result()


def main():
    conn_info = {"driver": "x", "url": "y", "user": "u", "password": "p", "jar": "z"}
    df = pull_members(conn_info)
    load_members(df, "acme-analytics-dev")


if __name__ == "__main__":
    main()
