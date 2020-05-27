"""
"""

from __future__ import absolute_import

import argparse
from datetime import datetime
import json
import logging
import re

import apache_beam as beam
from apache_beam import pvalue
from apache_beam.io import ReadFromText
from apache_beam.io import WriteToText
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions

from datasteward_df import common
from datasteward_df import negative_ages


def run(argv=None, save_main_session=True):
    """Main entry point; defines and runs the pipeline."""

    parser = argparse.ArgumentParser()
    parser.add_argument('--from-bigquery',
                        dest='from_bigquery',
                        const=True,
                        default=False,
                        nargs='?',
                        help='Whether to load from BigQuery')
    parser.add_argument('--to-bigquery',
                        dest='to_bigquery',
                        const=True,
                        default=False,
                        nargs='?',
                        help='Whether to load to BigQuery')
    known_args, pipeline_args = parser.parse_known_args(argv)
    pipeline_args.extend([
        '--project=aou-res-curation-test',
        '--service-account=dataflow-test@aou-res-curation-test.iam.gserviceaccount.com',
        '--staging_location=gs://dataflow-test-dc-864/staging',
        '--temp_location=gs://dataflow-test-dc-864/tmp',
        '--job_name=curation-prototype', '--region=us-central1',
        '--network=dataflow-test-dc-864'
    ])

    # We use the save_main_session option because one or more DoFn's in this
    # workflow rely on global context (e.g., a module imported at module level).
    pipeline_options = PipelineOptions(pipeline_args)
    pipeline_options.view_as(SetupOptions).save_main_session = save_main_session
    with beam.Pipeline(options=pipeline_options) as p:

        table_prefix = 'unioned_ehr'

        # Read all of the EHR inputs, into a dictionary of:
        #   table -> PCollection of table rows
        combined_by_domain = {}
        for tbl in ['person', 'measurement', 'condition_occurrence']:
            if known_args.from_bigquery:
                combined_by_domain[tbl] = (p | f"{tbl}" >> beam.io.Read(
                    beam.io.BigQuerySource(
                        query=
                        f"SELECT * FROM `aou-res-curation-test.synthea_ehr_ops_20200513.{table_prefix}_{tbl}` LIMIT 20",
                        use_standard_sql=True)))
            else:
                # TODO: FIX!
                combined_by_domain[tbl] = (
                    p | f"read {tbl}" >> ReadFromText(f"test_data/{tbl}.json") |
                    f"{tbl} from JSON" >> beam.Map(json.loads))

        person_by_key = (
            combined_by_domain['person'] |
            'person by key' >> beam.Map(lambda p: (p['person_id'], p)))
        for tbl in ['measurement', 'condition_occurrence']:
            by_person = (combined_by_domain[tbl] | f"{tbl} by person id" >>
                         beam.Map(lambda row: (row['person_id'], row)))
            combined_by_domain[tbl] = ({
                tbl: by_person,
                'person': person_by_key
            } | f"{tbl} cogrouped" >> beam.CoGroupByKey() | beam.ParDo(
                negative_ages.DropNegativeAges(tbl)))

        for domain, data in combined_by_domain.items():
            if known_args.to_bigquery:
                data | f"output for {domain}" >> beam.io.WriteToBigQuery(
                    table_spec,
                    schema=table_schema,
                    write_disposition=beam.io.BigQueryDisposition.
                    WRITE_TRUNCATE,
                    create_disposition=beam.io.BigQueryDisposition.
                    CREATE_IF_NEEDED)
            else:
                data | f"output for {domain}" >> beam.io.WriteToText(
                    f"out/{domain}.txt")


if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    run()
