import io
import uuid
import boto3
from typing import List, Optional

import dbt
import agate
from concurrent.futures import Future

from dbt.adapters.base import available
from dbt.adapters.base.relation import BaseRelation
from dbt.adapters.base.column import Column
from dbt.adapters.sql import SQLAdapter
from dbt.adapters.glue import GlueConnectionManager
from dbt.adapters.glue.gluedbapi import GlueConnection
from dbt.adapters.glue.relation import SparkRelation
from dbt.exceptions import NotImplementedException
from dbt.adapters.base.impl import catch_as_completed
from botocore.exceptions import ClientError

from dbt.utils import executor

from dbt.logger import GLOBAL_LOGGER as logger


class GlueAdapter(SQLAdapter):
    ConnectionManager = GlueConnectionManager
    Relation = SparkRelation

    relation_type_map = {'EXTERNAL_TABLE': 'table',
                         'MANAGED_TABLE': 'table',
                         'VIRTUAL_VIEW': 'view',
                         'table': 'table',
                         'view': 'view',
                         'cte': 'cte',
                         'materializedview': 'materializedview'}

    HUDI_METADATA_COLUMNS = [
        '_hoodie_commit_time',
        '_hoodie_commit_seqno',
        '_hoodie_record_key',
        '_hoodie_partition_path',
        '_hoodie_file_name'
    ]

    @classmethod
    def date_function(cls) -> str:
        return 'current_timestamp()'

    def list_schemas(self, database: str) -> List[str]:
        """
        Schemas in SQLite are attached databases
        """
        logger.debug("list_schemas called")
        results = self.connections.execute("show databases", fetch=True)
        schemas = [row[0] for row in results[1]]
        return schemas

    def list_relations_without_caching(self, schema_relation: BaseRelation):
        logger.debug("list_relations_without_caching called")
        connection = self.connections.get_thread_connection()
        client = connection.handle.client
        relations = []
        try:
            response = client.get_tables(
                DatabaseName=schema_relation.schema,
            )
            for table in response.get("TableList", []):
                relations.append(self.Relation.create(
                    schema=schema_relation.schema,
                    identifier=table.get("Name"),
                    type=self.relation_type_map.get(table.get("TableType")),
                ))
        except Exception as e:
            logger.debug(e)
            logger.debug("list_relations_without_caching exception")

        logger.debug("list_relations_without_caching ended")
        return relations

    @classmethod
    def convert_text_type(cls, agate_table, col_idx):
        logger.debug("convert_text_type called")
        return "string"

    @classmethod
    def convert_number_type(cls, agate_table, col_idx):
        logger.debug("convert_number_type called")
        decimals = agate_table.aggregate(agate.MaxPrecision(col_idx))
        return "double" if decimals else "bigint"

    @classmethod
    def convert_date_type(cls, agate_table, col_idx):
        logger.debug("convert_date_type called")
        return "date"

    @classmethod
    def convert_time_type(cls, agate_table, col_idx):
        logger.debug("convert_time_type called")
        return "time"

    @classmethod
    def convert_datetime_type(cls, agate_table, col_idx):
        logger.debug("convert_datetime_type called")
        return "timestamp"

    def check_schema_exists(self, database: str, schema: str) -> bool:
        logger.debug("check_schema_exists called")
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        client = boto3.client("glue", region_name=session.credentials.region)
        try:
            client.get_database(Name=schema)
            return True
        except:
            return False

    def get_relation(self, database, schema, identifier):
        logger.debug("get_relation called")
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        client = boto3.client("glue", region_name=session.credentials.region)
        relations = []
        try:
            response = client.get_table(
                DatabaseName=schema,
                Name=identifier
            )
            logger.debug("debug type")
            logger.debug(response.get("Table", {}).get("TableType", "Table"))
            relations.append(self.Relation.create(
                schema=schema,
                identifier=identifier,
                type=self.relation_type_map.get(response.get("Table", {}).get("TableType", "Table"))
            ))
            logger.debug("schema : " + schema)
            logger.debug("identifier : " + identifier)
            logger.debug("type : " + self.relation_type_map.get(response.get("Table", {}).get("TableType", "Table")))
            return relations
        except Exception as e:
            logger.debug(f"relation {schema}.{identifier} not found")
            return None

    @available
    def drop_view(self, relation: BaseRelation):
        logger.debug("drop_view called")
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        code = f'''DROP VIEW IF EXISTS {relation.schema}.{relation.name}'''
        cursor = session.cursor()
        try:
            cursor.execute(code)
        except Exception as e:
            logger.debug(e)
            logger.debug("drop_view exception")
            logger.debug("relation schema : " + relation.schema)
            logger.debug("relation identfier : " + relation.name)

    @available
    def drop_relation(self, relation: BaseRelation):
        logger.debug("drop_view called")
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        client = boto3.client("glue", region_name=session.credentials.region)
        try:
            response = client.delete_table(
                DatabaseName=relation.schema,
                Name=relation.identifier
            )
        except Exception as e:
            return None

    def rename_relation(self, from_relation, to_relation):
        logger.debug("rename_relation called")
        logger.debug("rename " + from_relation.schema + " to " + to_relation.identifier)
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        code = f'''
        --pyspark
        df=spark.sql("""select * from {from_relation.schema}.{from_relation.name}""")
        df.registerTempTable("df")
        df = df.coalesce(1)
        table_name = '{to_relation.schema}.{to_relation.name}'
        writer = (
                        df.write.mode("append")
                        .format("parquet")
                        .option("path", "{session.credentials.location}/{to_relation.schema}/{to_relation.name}/")
                    )
        writer.saveAsTable(table_name, mode="append")
        '''
        cursor = session.cursor()
        try:
            cursor.execute(code)
        except Exception as e:
            logger.debug(e)
            logger.debug("rename_relation exception")

    def get_columns_in_relation(self, relation: BaseRelation):
        logger.debug("get_columns_in_relation called")
        logger.debug(f"Command launched: describe {relation.schema}.{relation.identifier}")
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        code = f'''describe {relation.schema}.{relation.identifier}'''
        cursor = session.cursor()
        columns = []
        try:
            cursor.execute(code)
            for record in cursor.fetchall():
                columns.append(
                    Column(column=record[0], dtype=record[1])
                )
        except Exception as e:
            logger.debug(e)
            logger.debug("get_columns_in_relation exception")

        # strip hudi metadata columns.
        columns = [x for x in columns
                   if x.name not in self.HUDI_METADATA_COLUMNS]

        return columns

    def drop_schema(self, relation: BaseRelation) -> None:
        logger.debug("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ")
        logger.debug("drop_schema  called :) ", relation.identifier)
        logger.debug(self.connections)
        connection = self.connections.get_thread_connection()

        if self.check_schema_exists(relation.database, relation.schema):
            try:
                connection.handle.client.delete_database(Name=relation.schema)
                logger.debug("Successfull deleted schema ", relation.schema)
                self.connections.cleanup_all()
                return True
            except Exception as e:
                self.connections.cleanup_all()
                logger.debug(e)
                logger.debug(" - - ")
                logger.exception(e)
                pass
        else:
            logger.debug("No schema to delete")
            self.connections.cleanup_all()
            logger.debug(logger.level)
            logger.debug(logger.level_name)
            pass

    def create_schema(self, relation: BaseRelation):
        logger.debug("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ")
        logger.debug("create_schema  called :) ", relation.identifier)
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        client = connection.handle.client
        lf = boto3.client("lakeformation", region_name=session.credentials.region)
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        account = identity.get("Account")
        try:
            client.get_database(Name=relation.schema)
        except Exception as e:
            # create when database does not exist
            logger.debug("location = ", session.credentials.location)
            client.create_database(
                DatabaseInput={
                    "Name": relation.schema,
                    'Description': 'test dbt database',
                    'LocationUri': session.credentials.location,
                }
            )
            Entries = []
            for i, role_arn in enumerate([session.credentials.role_arn]):
                Entries.append(
                    {
                        "Id": str(uuid.uuid4()),
                        "Principal": {"DataLakePrincipalIdentifier": role_arn},
                        "Resource": {
                            "Database": {
                                # 'CatalogId': AWS_ACCOUNT,
                                "Name": relation.schema,
                            }
                        },
                        "Permissions": [
                            "Alter".upper(),
                            "Create_table".upper(),
                            "Drop".upper(),
                            "Describe".upper(),
                        ],
                        "PermissionsWithGrantOption": [
                            "Alter".upper(),
                            "Create_table".upper(),
                            "Drop".upper(),
                            "Describe".upper(),
                        ],
                    }
                )
                Entries.append(
                    {
                        "Id": str(uuid.uuid4()),
                        "Principal": {"DataLakePrincipalIdentifier": role_arn},
                        "Resource": {
                            "Table": {
                                "DatabaseName": relation.schema,
                                "TableWildcard": {},
                                "CatalogId": account
                            }
                        },
                        "Permissions": [
                            "Select".upper(),
                            "Insert".upper(),
                            "Delete".upper(),
                            "Describe".upper(),
                            "Alter".upper(),
                            "Drop".upper(),
                        ],
                        "PermissionsWithGrantOption": [
                            "Select".upper(),
                            "Insert".upper(),
                            "Delete".upper(),
                            "Describe".upper(),
                            "Alter".upper(),
                            "Drop".upper(),
                        ],
                    }
                )
            lf.batch_grant_permissions(CatalogId=account, Entries=Entries)

    def get_catalog(self, manifest):
        logger.debug("get_catalog called")
        schema_map = self._get_catalog_schemas(manifest)
        if len(schema_map) > 1:
            dbt.exceptions.raise_compiler_error(
                f'Expected only one database in get_catalog, found '
                f'{list(schema_map)}'
            )

        with executor(self.config) as tpe:
            futures: List[Future[agate.Table]] = []
            for info, schemas in schema_map.items():
                for schema in schemas:
                    futures.append(tpe.submit_connected(
                        self, schema,
                        self._get_one_catalog, info, [schema], manifest
                    ))
            catalogs, exceptions = catch_as_completed(futures)
        return catalogs, exceptions

    def _get_one_catalog(
            self, information_schema, schemas, manifest,
    ) -> agate.Table:
        logger.debug("_get_one_catalog called with args")
        logger.debug(schemas)
        if len(schemas) != 1:
            dbt.exceptions.raise_compiler_error(
                f'Expected only one schema in glue _get_one_catalog, found '
                f'{schemas}'
            )

        schema_base_relation = BaseRelation.create(
            schema=list(schemas)[0]
        )

        logger.debug("++++++++++++++++ Schemas : " + schema_base_relation.schema)
        results = self.list_relations_without_caching(schema_base_relation)
        rows = []

        for relation_row in results:
            name = relation_row.name
            relation_type = relation_row.type

            table_info = self.get_columns_in_relation(relation_row)

            for table_row in table_info:
                rows.append([
                    information_schema.database,
                    schema_base_relation.schema,
                    name,
                    relation_type,
                    '',
                    '',
                    table_row.column,
                    '0',
                    table_row.dtype,
                    ''
                ])
                logger.debug("database : " + information_schema.database)
                logger.debug("schema : " + schema_base_relation.schema)
                logger.debug("name : " + name)
                logger.debug("relation_type : " + relation_type)
                logger.debug("table_row.column : " + table_row.column)
                logger.debug("table_row.dtype : " + table_row.dtype)

        column_names = [
            'table_database',
            'table_schema',
            'table_name',
            'table_type',
            'table_comment',
            'table_owner',
            'column_name',
            'column_index',
            'column_type',
            'column_comment'
        ]
        table = agate.Table(rows, column_names)

        results = self._catalog_filter_table(table, manifest)
        return results

    @available
    def create_csv_table(self, model, agate_table):
        logger.debug("create_csv_table called")
        logger.debug(model)
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        f = io.StringIO("")
        agate_table.to_json(f)
        code = f'''
--pyspark
csv={f.getvalue()}
df= spark.createDataFrame(csv)
df.registerTempTable("df")
df = df.coalesce(1)
table_name = '{model["schema"]}.{model["name"]}'
writer = (
                df.write.mode("append")
                .format("parquet")
                .option("path", "{session.credentials.location}/{model["database"]}/{model["schema"]}/{model["name"]}")
            )
writer.saveAsTable(table_name, mode="append")
SqlWrapper2.execute("""select * from {model["schema"]}.{model["name"]}""")
'''
        cursor = session.cursor()
        cursor.execute(code)

    def add_schema_to_cache(self, schema) -> str:
        logger.debug("add_schema_to_cache called")
        """Cache a new schema in dbt. It will show up in `list relations`."""
        if schema is None:
            name = self.nice_connection_name()
            dbt.exceptions.raise_compiler_error(
                'Attempted to cache a null schema for {}'.format(name)
            )
        if dbt.flags.USE_CACHE:
            self.cache.add_schema(None, schema)
        # so jinja doesn't render things
        return ''

    @available
    def describe_table(self, relation):
        logger.debug("describe_table " + relation.schema)
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        client = boto3.client("glue", region_name=session.credentials.region)
        relations = []
        try:
            response = client.get_table(
                DatabaseName=relation.schema,
                Name=relation.name
            )
            relations.append(self.Relation.create(
                schema=relation.schema,
                identifier=relation.name,
                type=self.relation_type_map.get(response.get("Table", {}).get("TableType", "Table"))
            ))
            logger.debug("table_name : " + relation.name)
            logger.debug("table type : " + self.relation_type_map.get(response.get("Table", {}).get("TableType", "Table")))
            return relations
        except Exception as e:
            return None

    @available
    def get_table_type(self, relation):
        logger.debug("get_table_type " + relation.schema)
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        client = boto3.client("glue", region_name=session.credentials.region)
        try:
            response = client.get_table(
                DatabaseName=relation.schema,
                Name=relation.name
            )
            type = self.relation_type_map.get(response.get("Table", {}).get("TableType", "Table"))
            logger.debug("table_name : " + relation.name)
            logger.debug("table type : " + type)
            return type
        except Exception as e:
            return None

    def get_rows_different_sql(
            self,
            relation_a: BaseRelation,
            relation_b: BaseRelation,
            column_names: Optional[List[str]] = None,
            except_operator: str = 'EXCEPT',
    ) -> str:
        """Generate SQL for a query that returns a single row with a two
        columns: the number of rows that are different between the two
        relations and the number of mismatched rows.
        """
        # This method only really exists for test reasons.
        names: List[str]
        if column_names is None:
            columns = self.get_columns_in_relation(relation_a)
            names = sorted((self.quote(c.name) for c in columns))
        else:
            names = sorted((self.quote(n) for n in column_names))
        columns_csv = ', '.join(names)

        sql = COLUMNS_EQUAL_SQL.format(
            columns=columns_csv,
            relation_a=str(relation_a),
            relation_b=str(relation_b),
        )

        return sql

    @available
    def hudi_merge_table(self, target_relation, request, primary_key, partition_key):
        logger.debug("hudi_merge_table called")
        logger.debug("hudi_merge_table to " + target_relation.schema + " to " + target_relation.identifier)
        connection: GlueConnectionManager = self.connections.get_thread_connection()
        session: GlueConnection = connection.handle
        glueClient = boto3.client('glue')
        isTableExists = False
        try:
            glueClient.get_table(DatabaseName=target_relation.schema, Name=target_relation.name)
            isTableExists = True
            logger.debug(target_relation.schema + '.' + target_relation.name + ' exists.')
        except ClientError as e:
            if e.response['Error']['Code'] == 'EntityNotFoundException':
                isTableExists = False
                logger.debug(
                    target_relation.schema + '.' + target_relation.name + ' does not exist. Table will be created.')

        head_code = f'''
        --pyspark
from pyspark.sql import SparkSession
from pyspark.sql.functions import *
spark = SparkSession.builder \
.config("spark.serializer", "org.apache.spark.serializer.KryoSerializer") \
.getOrCreate()
inputDf = spark.sql("""{request}""")
outputDf = inputDf.withColumn("update_hudi_ts",current_timestamp())
if outputDf.count() > 0:
    if {partition_key} is not None:
        outputDf = outputDf.withColumn(partitionKey, concat(lit(partitionKey + '='), col(partitionKey)))
'''

        if isTableExists:
            core_code = f'''
        combinedConf = {{'className' : 'org.apache.hudi', 'hoodie.datasource.hive_sync.use_jdbc':'false', 'hoodie.datasource.write.precombine.field': 'update_hudi_ts', 'hoodie.consistency.check.enabled': 'true', 'hoodie.datasource.write.recordkey.field': '{primary_key}', 'hoodie.table.name': '{target_relation.name}', 'hoodie.datasource.hive_sync.database': '{target_relation.schema}', 'hoodie.datasource.hive_sync.table': '{target_relation.name}', 'hoodie.datasource.hive_sync.enable': 'true', 'hoodie.datasource.write.partitionpath.field': '{partition_key}', 'hoodie.datasource.hive_sync.partition_extractor_class': 'org.apache.hudi.hive.MultiPartKeysValueExtractor', 'hoodie.datasource.hive_sync.partition_fields': '{partition_key}', 'hoodie.upsert.shuffle.parallelism': 20, 'hoodie.datasource.write.operation': 'upsert', 'hoodie.cleaner.policy': 'KEEP_LATEST_COMMITS', 'hoodie.cleaner.commits.retained': 10}}
        outputDf.write.format('org.apache.hudi').options(**combinedConf).mode('Append').save("{session.credentials.location}{target_relation.schema}/{target_relation.name}/")
    else:
        combinedConf = {{'className' : 'org.apache.hudi', 'hoodie.datasource.hive_sync.use_jdbc':'false', 'hoodie.datasource.write.precombine.field': 'update_hudi_ts', 'hoodie.consistency.check.enabled': 'true', 'hoodie.datasource.write.recordkey.field': '{primary_key}', 'hoodie.table.name': '{target_relation.name}', 'hoodie.datasource.hive_sync.database': '{target_relation.schema}', 'hoodie.datasource.hive_sync.table': '{target_relation.name}', 'hoodie.datasource.hive_sync.enable': 'true', 'hoodie.datasource.hive_sync.partition_extractor_class': 'org.apache.hudi.hive.NonPartitionedExtractor', 'hoodie.datasource.write.keygenerator.class': 'org.apache.hudi.keygen.NonpartitionedKeyGenerator', 'hoodie.upsert.shuffle.parallelism': 20, 'hoodie.datasource.write.operation': 'upsert', 'hoodie.cleaner.policy': 'KEEP_LATEST_COMMITS', 'hoodie.cleaner.commits.retained': 10}}
        outputDf.write.format('org.apache.hudi').options(**combinedConf).mode('Append').save("{session.credentials.location}{target_relation.schema}/{target_relation.name}/")
'''
        else:
            core_code = f'''
        combinedConf = {{'className' : 'org.apache.hudi', 'hoodie.datasource.hive_sync.use_jdbc':'false', 'hoodie.datasource.write.precombine.field': 'update_hudi_ts', 'hoodie.consistency.check.enabled': 'true', 'hoodie.datasource.write.recordkey.field': '{primary_key}', 'hoodie.table.name': '{target_relation.name}', 'hoodie.datasource.hive_sync.database': '{target_relation.schema}', 'hoodie.datasource.hive_sync.table': '{target_relation.name}', 'hoodie.datasource.hive_sync.enable': 'true', 'hoodie.datasource.write.partitionpath.field': '{partition_key}', 'hoodie.datasource.hive_sync.partition_extractor_class': 'org.apache.hudi.hive.MultiPartKeysValueExtractor', 'hoodie.datasource.hive_sync.partition_fields': '{partition_key}', 'hoodie.bulkinsert.shuffle.parallelism': 5, 'hoodie.datasource.write.operation': 'bulk_insert'}}
        outputDf.write.format('org.apache.hudi').options(**combinedConf).mode('Overwrite').save("{session.credentials.location}{target_relation.schema}/{target_relation.name}/")
    else:
        combinedConf = {{'className' : 'org.apache.hudi', 'hoodie.datasource.hive_sync.use_jdbc':'false', 'hoodie.datasource.write.precombine.field': 'update_hudi_ts', 'hoodie.consistency.check.enabled': 'true', 'hoodie.datasource.write.recordkey.field': '{primary_key}', 'hoodie.table.name': '{target_relation.name}', 'hoodie.datasource.hive_sync.database': '{target_relation.schema}', 'hoodie.datasource.hive_sync.table': '{target_relation.name}', 'hoodie.datasource.hive_sync.enable': 'true', 'hoodie.datasource.hive_sync.partition_extractor_class': 'org.apache.hudi.hive.NonPartitionedExtractor', 'hoodie.datasource.write.keygenerator.class': 'org.apache.hudi.keygen.NonpartitionedKeyGenerator', 'hoodie.bulkinsert.shuffle.parallelism': 5, 'hoodie.datasource.write.operation': 'bulk_insert'}}
        outputDf.write.format('org.apache.hudi').options(**combinedConf).mode('Overwrite').save("{session.credentials.location}{target_relation.schema}/{target_relation.name}/")
'''

        footer_code = f'''
spark.sql("""REFRESH TABLE {target_relation.schema}.{target_relation.name}""")
SqlWrapper2.execute("""SELECT * FROM {target_relation.schema}.{target_relation.name} LIMIT 1""")
'''
        code = head_code + core_code + footer_code

        cursor = session.cursor()
        cursor.execute(code)


# spark does something interesting with joins when both tables have the same
# static values for the join condition and complains that the join condition is
# "trivial". Which is true, though it seems like an unreasonable cause for
# failure! It also doesn't like the `from foo, bar` syntax as opposed to
# `from foo cross join bar`.
COLUMNS_EQUAL_SQL = '''
with diff_count as (
    SELECT
        1 as id,
        COUNT(*) as num_missing FROM (
            (SELECT {columns} FROM {relation_a} EXCEPT
             SELECT {columns} FROM {relation_b})
             UNION ALL
            (SELECT {columns} FROM {relation_b} EXCEPT
             SELECT {columns} FROM {relation_a})
        ) as a
), table_a as (
    SELECT COUNT(*) as num_rows FROM {relation_a}
), table_b as (
    SELECT COUNT(*) as num_rows FROM {relation_b}
), row_count_diff as (
    select
        1 as id,
        table_a.num_rows - table_b.num_rows as difference
    from table_a
    cross join table_b
)
select
    row_count_diff.difference as row_count_difference,
    diff_count.num_missing as num_mismatched
from row_count_diff
cross join diff_count
'''.strip()