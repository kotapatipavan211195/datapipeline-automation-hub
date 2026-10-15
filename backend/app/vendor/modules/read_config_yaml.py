"""
    read_config_yaml — read a pipeline's YAML configuration (one block per environment) and table definitions.

    Hub copy of /data/data_eng/py_scripts/modules/read_config_yaml.py (rebuilt from screenshots; keep identical).
"""

import logging
import sys  # noqa: F401  (kept as in the original module)

import yaml
from google.cloud import bigquery  # noqa: F401  (kept as in the original module)

# configure logger
logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("Cloud SQL PG Context Pull")
logger.setLevel(logging.INFO)


def read_configuration(environment:str, file:str="config.json")->dict:
    """
    Reads the configuration data from a YAML file for the specified environment.

    Args:
        environment (str): The environment for which configuration is needed (e.g., 'lab', 'value', 'qa').
        file (str): The filename and path of the YAML configuration file. Default is "config.json".

    Returns:
        dict: A dictionary containing the configuration data for the specified environment.

    Raises:
        RuntimeError: If an error occurs while reading the configuration file.
    """
    logger.info(f'Function {read_configuration.__name__} execution begin')
    try:
        with open(file, 'r') as config_file:
            config_data = yaml.load(config_file.read(), Loader=yaml.FullLoader)

        logger.info(f'Function {read_configuration.__name__} execution end')
        return config_data['gcp_projects'][environment]

    except Exception as e:
        s = str(e)
        raise RuntimeError(s)


def read_table_definition(file:str)->dict:
    """
    Reads the table definition and write disposition from a YAML file.

    Args:
        file (str): The filename and path of the YAML table definition file.

    Returns:
        dict: A dictionary containing the table definition for the destination BigQuery table.

    Raises:
        RuntimeError: If an error occurs while reading the YAML file.
    """
    logger.info(f'Function {read_table_definition.__name__} execution begin')
    try:
        with open(file, 'r') as table_file:
            table_data = yaml.safe_load(table_file)

        logger.info(f'Function {read_table_definition.__name__} execution end')
        return table_data['table_definition']

    except Exception as e:
        s = str(e)
        raise RuntimeError(s)
