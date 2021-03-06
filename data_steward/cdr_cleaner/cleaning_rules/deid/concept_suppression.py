import logging
from typing import List
from abc import abstractmethod
from google.cloud.bigquery.client import Client
from google.cloud.exceptions import GoogleCloudError

from resources import get_concept_id_fields, has_domain_table_id
from common import JINJA_ENV
from constants import bq_utils as bq_consts
import constants.cdr_cleaner.clean_cdr as cdr_consts
from cdr_cleaner.cleaning_rules.base_cleaning_rule import BaseCleaningRule, query_spec_list, \
    get_delete_empty_sandbox_tables_queries

LOGGER = logging.getLogger(__name__)

TABLE_ID = 'table_id'

GET_ALL_TABLES_QUERY_TEMPLATE = JINJA_ENV.from_string("""
SELECT
  table_id
FROM `{{project}}.{{dataset}}.__TABLES__`
WHERE table_id IN (
{% for table_name in table_names %}
    {% if loop.previtem is defined %}, {% else %}  {% endif %} '{{table_name}}'
{% endfor %}
)
""")


def get_tables_in_dataset(client: Client, project_id, dataset_id,
                          table_names) -> List[str]:
    """
    This function retrieves tables that exist in dataset for an inital list table_names . This 
    function raises GoogleCloudError if the query throws an error
    
    :param client: 
    :param project_id: 
    :param dataset_id: 
    :param table_names: 
    :return: a list of tables that exist in the given dataset
    """
    # The following makes sure the tables exist in the dataset
    query_job = client.query(
        GET_ALL_TABLES_QUERY_TEMPLATE.render(project=project_id,
                                             dataset=dataset_id,
                                             table_names=table_names))

    try:
        result = query_job.result()
        # Raise the Runtime Error if the errors are neither GoogleCloudError nor TimeoutError
        if query_job.errors:
            raise RuntimeError(result.errors)

        return [dict(row.items())[TABLE_ID] for row in result]

    except (GoogleCloudError, TimeoutError, RuntimeError) as e:
        # Catch GoogleCloudError and TimeoutError that could be raised by query_job.result()
        # Also catch the RuntimeError raised from the try block
        # Log the error and raise it again
        LOGGER.error(f"Error running job {result.job_id}: {e}")
        raise


class AbstractConceptSuppression(BaseCleaningRule):
    """
    Abstract class for creating concept suppression rules
    """

    SUPPRESSION_RECORD_QUERY_TEMPLATE = JINJA_ENV.from_string("""
    SELECT
      d.*
    FROM `{{project}}.{{dataset}}.{{domain_table}}` AS d
    LEFT JOIN `{{project}}.{{sandbox_dataset}}.{{sandbox_table}}` AS s
        ON d.{{domain_table}}_id = s.{{domain_table}}_id
    WHERE s.{{domain_table}}_id IS NULL
    """)

    def __init__(self, project_id, dataset_id, sandbox_dataset_id,
                 issue_numbers, description, affected_datasets,
                 affected_tables):
        """
        Initialize the class with proper info.

        Set the issue numbers, description and affected datasets.  As other
        tickets may affect this SQL, append them to the list of Jira Issues.
        DO NOT REMOVE ORIGINAL JIRA ISSUE NUMBERS!
        """

        affected_tables = [
            table_name for table_name in affected_tables
            if (get_concept_id_fields(table_name) and
                has_domain_table_id(table_name))
        ]

        super().__init__(issue_numbers=issue_numbers,
                         description=description,
                         affected_datasets=affected_datasets,
                         project_id=project_id,
                         dataset_id=dataset_id,
                         sandbox_dataset_id=sandbox_dataset_id,
                         affected_tables=affected_tables)

    def setup_rule(self, client: Client, *args, **keyword_args):
        # The following makes sure the tables exist in the dataset
        try:
            self.affected_tables = get_tables_in_dataset(
                client, self.project_id, self.dataset_id, self.affected_tables)
        except GoogleCloudError as error:
            LOGGER.error(error)
            raise

    def get_sandbox_tablenames(self):
        return [
            self.sandbox_table_for(affected_table)
            for affected_table in self.affected_tables
        ]

    @abstractmethod
    def get_sandbox_query(self, table_name):
        """
        Get the sandbox query that identifies records that are associated with a suppressing 
        concept, which will be sandboxed 
        
        :param table_name: 
        :return: 
        """
        pass

    def get_suppression_query(self, table_name):
        """
        Get the suppression query that deletes records that are in the corresponding sandbox table
        
        :param table_name: 
        :return: 
        """
        suppression_record_query = self.SUPPRESSION_RECORD_QUERY_TEMPLATE.render(
            project=self.project_id,
            dataset=self.dataset_id,
            sandbox_dataset=self.sandbox_dataset_id,
            domain_table=table_name,
            sandbox_table=self.sandbox_table_for(table_name))

        return {
            cdr_consts.QUERY: suppression_record_query,
            cdr_consts.DESTINATION_DATASET: self.dataset_id,
            cdr_consts.DISPOSITION: bq_consts.WRITE_TRUNCATE,
            cdr_consts.DESTINATION_TABLE: table_name
        }

    def get_query_specs(self, *args, **keyword_args) -> query_spec_list:
        # Queries for sandboxing the records
        sandbox_queries = [
            self.get_sandbox_query(table_name)
            for table_name in self.affected_tables
        ]

        # Queries for dropping records based on the sandboxed records
        queries = [
            self.get_suppression_query(table_name)
            for table_name in self.affected_tables
        ]

        # Clean up the empty sandbox tables
        delete_empty_sandbox_queries = get_delete_empty_sandbox_tables_queries(
            self.project_id, self.sandbox_dataset_id,
            self.get_sandbox_tablenames())

        return sandbox_queries + queries + delete_empty_sandbox_queries


class AbstractBqLookupTableConceptSuppression(AbstractConceptSuppression):
    """
    This class is to be extended when creating a concept_id suppression table
    """
    BQ_LOOKUP_TABLE_SANDBOX_QUERY_TEMPLATE = JINJA_ENV.from_string("""
    SELECT
      d.*
    FROM `{{project}}.{{dataset}}.{{domain_table}}` AS d
    {% for concept_field in concept_fields %}
    LEFT JOIN `{{project}}.{{sandbox_dataset}}.{{suppression_concept}}` AS s{{loop.index}}
      ON d.{{concept_field}} = s{{loop.index}}.concept_id 
    {% endfor %}
    WHERE COALESCE(
    {% for concept_field in concept_fields %}
        {% if loop.previtem is defined %}, {% else %}  {% endif %} s{{loop.index}}.concept_id
    {% endfor %}) IS NOT NULL
    """)

    def __init__(self, project_id, dataset_id, sandbox_dataset_id,
                 issue_numbers, description, affected_datasets, affected_tables,
                 concept_suppression_lookup_table):
        """
        Initialize the class with proper info.

        Set the issue numbers, description and affected datasets.  As other
        tickets may affect this SQL, append them to the list of Jira Issues.
        DO NOT REMOVE ORIGINAL JIRA ISSUE NUMBERS!
        """

        super().__init__(issue_numbers=issue_numbers,
                         description=description,
                         affected_datasets=affected_datasets,
                         project_id=project_id,
                         dataset_id=dataset_id,
                         sandbox_dataset_id=sandbox_dataset_id,
                         affected_tables=affected_tables)

        self._concept_suppression_lookup_table = concept_suppression_lookup_table

    @property
    def concept_suppression_lookup_table(self):
        """
        Return the issue_urls instance variable.
        """
        return self._concept_suppression_lookup_table

    def setup_rule(self, client: Client, *args, **keyword_args):
        # Pass it up to the super class
        super().setup_rule(client, *args, **keyword_args)
        # Create the suppression lookup table
        self.create_suppression_lookup_table(client)

    @abstractmethod
    def create_suppression_lookup_table(self, client: Client):
        """
        Build the concept suppression lookup table 
        
        :param client: Bigquery client
        :return: 
        """
        pass

    def get_sandbox_query(self, table_name):
        """
        Sandbox records in the given table whose concept id fields contain any concepts in the 
        suppression concept table 

        :param table_name: 
        :return: 
        """
        suppression_record_sandbox_query = self.BQ_LOOKUP_TABLE_SANDBOX_QUERY_TEMPLATE.render(
            project=self.project_id,
            dataset=self.dataset_id,
            sandbox_dataset=self.sandbox_dataset_id,
            domain_table=table_name,
            concept_fields=get_concept_id_fields(table_name),
            suppression_concept=self.concept_suppression_lookup_table)

        return {
            cdr_consts.QUERY: suppression_record_sandbox_query,
            cdr_consts.DESTINATION_TABLE: self.sandbox_table_for(table_name),
            cdr_consts.DISPOSITION: bq_consts.WRITE_TRUNCATE,
            cdr_consts.DESTINATION_DATASET: self.sandbox_dataset_id
        }


class AbstractInMemoryLookupTableConceptSuppression(AbstractConceptSuppression):
    """
    This class is intended to be extended when providing a short list of concept_ids for suppression
    """
    IN_MEMORY_SANDBOX_QUERY_TEMPLATE = JINJA_ENV.from_string("""
    WITH suppressed_concepts AS 
    (
        SELECT
          concept_id
        FROM UNNEST ([
        {% for concept_id in suppressed_concept_ids %}
            {% if loop.previtem is defined %}, {% else %}  {% endif %} {{concept_id}}
        {% endfor %}]) AS concept_id
    )
    
    SELECT
      d.*
    FROM `{{project}}.{{dataset}}.{{domain_table}}` AS d
    {% for concept_field in concept_fields %}
    LEFT JOIN suppressed_concepts AS s{{loop.index}}
      ON d.{{concept_field}} = s{{loop.index}}.concept_id 
    {% endfor %}
    WHERE COALESCE(
    {% for concept_field in concept_fields %}
        {% if loop.previtem is defined %}, {% else %}  {% endif %} s{{loop.index}}.concept_id
    {% endfor %}) IS NOT NULL
    """)

    @abstractmethod
    def get_suppressed_concept_ids(self):
        pass

    def get_sandbox_query(self, table_name):
        """
        Sandbox records in the given table whose concept id fields contain any concepts in the 
        list of suppressed concepts 

        :param table_name: 
        :return: 
        """
        suppression_record_sandbox_query = self.IN_MEMORY_SANDBOX_QUERY_TEMPLATE.render(
            project=self.project_id,
            dataset=self.dataset_id,
            sandbox_dataset=self.sandbox_dataset_id,
            domain_table=table_name,
            concept_fields=get_concept_id_fields(table_name),
            suppressed_concept_ids=self.get_suppressed_concept_ids())

        return {
            cdr_consts.QUERY: suppression_record_sandbox_query,
            cdr_consts.DESTINATION_TABLE: self.sandbox_table_for(table_name),
            cdr_consts.DISPOSITION: bq_consts.WRITE_TRUNCATE,
            cdr_consts.DESTINATION_DATASET: self.sandbox_dataset_id
        }
