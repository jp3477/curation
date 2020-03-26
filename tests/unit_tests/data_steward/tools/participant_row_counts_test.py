import re
import unittest

import pandas as pd

import common
from tools import participant_row_counts as prc
from constants.tools import participant_row_counts as consts


class ParticipantPrevalenceTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        print('**************************************************************')
        print(cls.__name__)
        print('**************************************************************')

    def setUp(self):
        self.project_id = 'project_id'
        self.dataset_id = 'dataset_id'
        self.ehr_dataset_id = 'ehr_dataset_id'
        self.pid_table_str = 'pid_project_id.sandbox_dataset_id.pid_table_id'
        self.hpo_id = 'fake'
        self.TABLE_REGEX = re.compile(
            r'`' + self.project_id + r'\.' + self.dataset_id + r'\.(.*)`',
            re.MULTILINE | re.IGNORECASE)

        # common tables across datasets
        self.mapped_cdm_tables = [
            common.OBSERVATION, common.VISIT_OCCURRENCE, common.MEASUREMENT,
            common.OBSERVATION_PERIOD, common.CONDITION_OCCURRENCE
        ]
        self.unmapped_cdm_tables = [common.PERSON, common.DEATH]
        self.mapping_tables = [
            common.MAPPING_PREFIX + table for table in self.mapped_cdm_tables
        ]
        self.ext_tables = [
            table + common.EXT_SUFFIX for table in self.mapped_cdm_tables
        ]
        self.cdm_tables = self.mapped_cdm_tables + self.unmapped_cdm_tables
        self.other_tables = [
            common.MAPPING_PREFIX + 'drug_source',
            'site' + common.MAPPING_PREFIX
        ]

        self.list_of_dicts = [{
            consts.TABLE_NAME: table,
            consts.COLUMN_NAME: consts.PERSON_ID
        } for table in self.cdm_tables] + [{
            consts.TABLE_NAME: table,
            consts.COLUMN_NAME: consts.TABLE_ID
        } for table in self.mapping_tables + self.ext_tables] + [{
            consts.TABLE_NAME: table,
            consts.COLUMN_NAME: consts.TABLE_ID
        } for table in self.other_tables]

        self.table_df = pd.DataFrame(self.list_of_dicts)

        self.pids_list = [1, 2, 3, 4]

    def test_get_pid_sql_expr(self):
        expected = '({pids})'.format(
            pids=', '.join([str(pid) for pid in self.pids_list]))
        actual = prc.get_pid_sql_expr(self.pids_list)
        self.assertEqual(expected, actual)

        self.assertRaises(ValueError, prc.get_pid_sql_expr, self.project_id)
        self.assertRaises(ValueError, prc.get_pid_sql_expr,
                          self.project_id + '.' + self.dataset_id)

    def test_get_cdm_table(self):
        expected = common.AOU_REQUIRED
        mapping_tables = [
            common.MAPPING_PREFIX + table for table in common.AOU_REQUIRED
        ]
        ext_tables = [
            table + common.EXT_SUFFIX for table in common.AOU_REQUIRED
        ]
        for table in mapping_tables:
            cdm_table = prc.get_cdm_table(table)
            self.assertIn(cdm_table, expected)
        for table in ext_tables:
            cdm_table = prc.get_cdm_table(table)
            self.assertIn(cdm_table, expected)

    def test_get_combined_deid_query(self):
        pid_sql_expr = prc.get_pid_sql_expr(self.pids_list)

        actual = prc.get_combined_deid_query(self.project_id, self.dataset_id,
                                             pid_sql_expr, self.table_df)
        queries = actual.split(consts.UNION_ALL)

        for query in queries:
            tables = self.TABLE_REGEX.findall(query)
            self.assertLessEqual(len(tables), 2)
            self.assertGreaterEqual(len(tables), 1)
            if len(tables) == 2:
                table = tables[0]
                map_table = tables[1]
                self.assertIn(table, self.mapped_cdm_tables)
                self.assertNotIn(table, self.unmapped_cdm_tables)
                self.assertIn(map_table, self.mapping_tables + self.ext_tables)
            elif len(tables) == 1:
                table = tables[0]
                self.assertIn(table, self.unmapped_cdm_tables)
                self.assertNotIn(table, self.mapped_cdm_tables)
                if table == common.PERSON:
                    self.assertIn('0 AS ehr_count', query)
                elif table == common.DEATH:
                    self.assertIn('COUNT(*) AS ehr_count', query)

    def test_get_dataset_query(self):
        pid_sql_expr = prc.get_pid_sql_expr(self.pid_table_str)

        actual = prc.get_dataset_query(self.project_id, self.dataset_id,
                                       pid_sql_expr, self.table_df)
        queries = actual.split(consts.UNION_ALL)

        for query in queries:
            tables = self.TABLE_REGEX.findall(query)
            self.assertEqual(len(tables), 1)
            table = tables[0]
            self.assertIn(table, self.cdm_tables)
            self.assertIn('COUNT(*) AS ehr_count', query)

    def test_get_ehr_query(self):
        pid_sql_expr = prc.get_pid_sql_expr(self.pid_table_str)

        # ehr dataset tables
        participant_tables = [
            common.PII_NAME, common.PARTICIPANT_MATCH, common.PII_ADDRESS
        ]
        unioned_ehr_tables = [
            common.UNIONED_EHR + '_' + table for table in self.cdm_tables
        ]
        hpo_tables = [
            self.hpo_id + '_' + table
            for table in self.cdm_tables + participant_tables
        ]
        ehr_tables = hpo_tables + unioned_ehr_tables

        ehr_list_of_dicts = [{
            consts.TABLE_NAME: table,
            consts.COLUMN_NAME: consts.PERSON_ID
        } for table in ehr_tables]
        ehr_table_df = pd.DataFrame(ehr_list_of_dicts)

        actual = prc.get_ehr_query(self.project_id, self.dataset_id,
                                   pid_sql_expr, self.hpo_id, ehr_table_df)
        queries = actual.split(consts.UNION_ALL)

        for query in queries:
            tables = self.TABLE_REGEX.findall(query)
            self.assertEqual(len(tables), 1)
            table = tables[0]
            self.assertIn(table, ehr_tables)
            self.assertIn('COUNT(*) AS ehr_count', query)

    def test_get_dataset_type(self):
        self.assertEqual(prc.get_dataset_type('unioned_ehr_4023498'),
                         common.UNIONED_EHR)
        self.assertNotEqual(prc.get_dataset_type('unioned_ehr_4023498'),
                            common.COMBINED)
        self.assertNotEqual(prc.get_dataset_type('unioned_ehr_4023498'),
                            common.DEID)
        self.assertNotEqual(prc.get_dataset_type('unioned_ehr_4023498'),
                            common.EHR)
        self.assertNotEqual(prc.get_dataset_type('unioned_ehr_4023498'),
                            common.OTHER)

        self.assertEqual(prc.get_dataset_type('5349850'), common.OTHER)
        self.assertNotEqual(prc.get_dataset_type('5349850'), common.COMBINED)
        self.assertNotEqual(prc.get_dataset_type('5349850'), common.DEID)
        self.assertNotEqual(prc.get_dataset_type('5349850'), common.EHR)
        self.assertNotEqual(prc.get_dataset_type('5349850'), common.UNIONED_EHR)

        self.assertEqual(prc.get_dataset_type('combined_deid_53521'),
                         common.DEID)
        self.assertNotEqual(prc.get_dataset_type('combined_deid_53521'),
                            common.COMBINED)
        self.assertNotEqual(prc.get_dataset_type('combined_deid_53521'),
                            common.UNIONED_EHR)
        self.assertNotEqual(prc.get_dataset_type('combined_deid_53521'),
                            common.EHR)
        self.assertNotEqual(prc.get_dataset_type('combined_deid_53521'),
                            common.OTHER)

        self.assertEqual(prc.get_dataset_type('combined_dbrowser_562'),
                         common.COMBINED)
        self.assertNotEqual(prc.get_dataset_type('combined_dbrowser_562'),
                            common.DEID)
        self.assertNotEqual(prc.get_dataset_type('combined_dbrowser_562'),
                            common.UNIONED_EHR)
        self.assertNotEqual(prc.get_dataset_type('combined_dbrowser_562'),
                            common.EHR)
        self.assertNotEqual(prc.get_dataset_type('combined_dbrowser_562'),
                            common.OTHER)

        self.assertEqual(prc.get_dataset_type('ehr_43269'), common.EHR)
        self.assertNotEqual(prc.get_dataset_type('ehr_43269'), common.DEID)
        self.assertNotEqual(prc.get_dataset_type('ehr_43269'),
                            common.UNIONED_EHR)
        self.assertNotEqual(prc.get_dataset_type('ehr_43269'), common.COMBINED)
        self.assertNotEqual(prc.get_dataset_type('ehr_43269'), common.OTHER)

    def test_fetch_args(self):
        parser = prc.fetch_parser()

        expected = self.pids_list
        args = parser.parse_args([
            '-p', self.project_id, '-o', self.hpo_id, 'pid_list', '1', '2', '3',
            '4'
        ])
        actual = args.pid_source
        self.assertEqual(expected, actual)

        expected = self.pid_table_str
        args = parser.parse_args([
            '-p', self.project_id, '-o', self.hpo_id, 'pid_table',
            self.pid_table_str
        ])
        actual = args.pid_source
        self.assertEqual(expected, actual)
