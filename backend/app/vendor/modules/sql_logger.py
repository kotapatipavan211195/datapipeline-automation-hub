"""
    sql_logger — capture the static SQL executed in Python pipelines in BigQuery
    (<project>.pipeline_audit.pipeline_sql_log).

    Hub copy of /data/data_eng/py_scripts/modules/sql_logger.py (rebuilt from screenshots; keep identical).
"""

import os
import uuid
import logging
from google.cloud import bigquery

# Configure logger
CONFIG = {
    'SCRIPT_NAME': os.path.basename(__file__),
    'LOG_FORMAT': '%(asctime)s-%(levelname)s-%(message)s',
    'LOG_LEVEL': logging.INFO
}

logging.basicConfig(level=CONFIG['LOG_LEVEL'], format=CONFIG['LOG_FORMAT'])
logger = logging.getLogger(CONFIG['SCRIPT_NAME'])

# Get dag_id and task_id from environment variables
dag_id = os.getenv("DAG_ID", "")
task_id = os.getenv("TASK_ID", "")

class CaptureSQL:
    """
    This class is used to capture the static SQL executed in Python pipelines.

    Args:
        project_id (str): Google project ID.
        credentials (google.auth.credentials.Credentials): Google client credentials.
        script_id (str): The name of the script that executes the SQL.
        environment (str): The environment argument passed to the script.

    """

    def __init__(self, project_id:str, credentials, script_id:str, environment:str):

        # Attributes
        self._project_id = project_id
        self._credentials = credentials
        self._script_id = script_id
        self._environment = environment

        # Instance variables
        self._log_id = str(uuid.uuid1())
        self._dag_id = dag_id
        self._task_id = task_id
        self._sql_step_order = 1

        logger.info(f'SQL will be logged to BigQuery table: {self._project_id}.pipeline_audit.pipeline_sql_log')
        logger.info(f'SQL log id: {self._log_id}')

    def write_to_log(self, sql_step_id:str, sql_ddl:str, status:str, job_id:str=None):
        """
        Write the SQL DDL to a BigQuery table.

        Destination table: *value-gcp.pipeline_audit.pipeline_sql_log*

        Parameters:
            sql_step_id (str): The name of the SQL statement.
            sql_ddl (str): The DDL for the SQL statement.
            status ({'success', 'failure'}): Status of the executed SQL. Allowed values are: `success`, `failure`.
            job_id (str | optional): The job ID for SQL executed using the BigQuery client.

        Raises:
            ValueError: Raised if an invalid value is passed to the `status` parameter.
            RuntimeError: Raised if an exception occurs during execution.

        """

        # Format the status value
        status = status.lower()

        # Validate status value
        if status not in ('success', 'failure'):
            raise ValueError(f'Invalid value for status: {status}')

        bigquery_client = bigquery.Client(project=self._project_id, credentials=self._credentials)

        insert_query = f"""
                        INSERT INTO `{self._project_id}.pipeline_audit.pipeline_sql_log`
                        VALUES(
                            @dag_id,
                            @task_id,
                            @script_id,
                            @sql_step_id,
                            @sql_step_order,
                            @sql_ddl,
                            @job_id,
                            @status,
                            @environment,
                            @log_id,
                            CURRENT_DATETIME()
                        );
                        """

        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("dag_id", "STRING", self._dag_id),
            bigquery.ScalarQueryParameter("task_id", "STRING", self._task_id),
            bigquery.ScalarQueryParameter("script_id", "STRING", self._script_id),
            bigquery.ScalarQueryParameter("sql_step_id", "STRING", sql_step_id),
            bigquery.ScalarQueryParameter("sql_step_order", "INT64", self._sql_step_order),
            bigquery.ScalarQueryParameter("sql_ddl", "STRING", sql_ddl),
            bigquery.ScalarQueryParameter("job_id", "STRING", job_id),
            bigquery.ScalarQueryParameter("status", "STRING", status),
            bigquery.ScalarQueryParameter("environment", "STRING", self._environment),
            bigquery.ScalarQueryParameter("log_id", "STRING", self._log_id)
            ]
        )

        try:
            # Capture executed SQL in BigQuery table for logging
            insert_job = bigquery_client.query(query=insert_query, job_config=job_config)
            insert_job.result()

            self._sql_step_order += 1

        except Exception as e:
            s = str(e)
            raise RuntimeError(s)
