#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Author: P55555
# Reviewed by: P66666
# Date: 03/15/2023
# Purpose: Refreshes the daily claims summary
#          for the claims dashboard.
# Change log:
#   2024-06-01 P55555 added the claim_type column
import logging
import os

from google.cloud import bigquery
from google.oauth2 import service_account

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/data/keys/claims-sa.json"
logger = logging.getLogger("claims_summary")


def get_client(key_path):
    """Build the BigQuery client from the service-account key"""
    credentials = service_account.Credentials.from_service_account_file(key_path)
    return bigquery.Client(credentials=credentials, project=credentials.project_id)


def build_claims_summary(client, write_project):
    """Rebuild the daily claims summary"""
    sql = f"""
        CREATE OR REPLACE TABLE `{write_project}.claims_mart.daily_summary` AS
        SELECT claim_date, claim_type, COUNT(*) AS claims
        FROM `{write_project}.claims.claims`
        GROUP BY claim_date, claim_type
    """
    client.query(sql).result()
    logger.info("claims summary rebuilt")


def main():
    client = get_client("/data/keys/claims-sa.json")
    build_claims_summary(client, "acme-claims-dev")


if __name__ == "__main__":
    main()
