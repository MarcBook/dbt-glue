from dataclasses import dataclass
from dbt import exceptions as dbterrors
from dbt.logger import GLOBAL_LOGGER as logger
import boto3
from botocore.exceptions import ClientError
from waiter import wait
from dbt.adapters.glue.gluedbapi.cursor import GlueCursor, GlueDictCursor
from dbt.adapters.glue.credentials import GlueCredentials
from dbt.adapters.glue.gluedbapi.commons import GlueStatement
import uuid

class GlueSessionState:
    READY = "READY"
    FAILED = "FAILED"
    PROVISIONING = "PROVISIONING"
    CLOSED = "CLOSED"


@dataclass
class GlueConnection:

    def __init__(self, credentials: GlueCredentials):
        self.credentials = credentials
        self._client = None
        self._session = None
        self._state = None

    def connect(self):
        logger.debug("GlueConnection connect called")
        if not self.credentials.session_id:
            logger.debug("No session go to start session")
            self._start_session()
        else:
            self._session = {
                "Session": {"Id": self.credentials.session_id}
            }
            logger.debug("Existing session with status : " + self.state)
            if self.state == GlueSessionState.CLOSED:
                self._session = self._start_session()

        self._init_session()
        self.credentials.session_id = self.session_id
        return self.session_id

    def _start_session(self):
        logger.debug("GlueConnection _start_session called")

        if (self.credentials.extra_jars is not None):
            args = {
                "--enable-glue-datacatalog": "true",
                "--spark.sql.crossJoin.enabled": "true",
                "--extra-jars": f"{self.credentials.extra_jars}"
            }
        else:
            args = {
                "--enable-glue-datacatalog": "true",
                "--spark.sql.crossJoin.enabled": "true"
            }
            
        additional_args = {}
        additional_args["NumberOfWorkers"] = self.credentials.workers
        additional_args["WorkerType"] = self.credentials.worker_type

        session_uuid = uuid.uuid4()
        session_uuidStr = str(session_uuid)

        try:
            self._session = self.client.create_session(
                Id=f"dbt-glue-{session_uuidStr}",
                Role=self.credentials.role_arn,
                DefaultArguments=args,
                Command={
                    "Name": "glueetl",
                    "PythonVersion": "3"
                },
                **additional_args)
        except ClientError:
            self._state = GlueSessionState.FAILED
            raise dbterrors.FailedToConnectException

        for elapsed in wait(1):
            if self.state == GlueSessionState.READY:
                return self._session
            if elapsed > self.credentials.session_provisionning_timeout_in_seconds:
                raise TimeoutError

    def _init_session(self):
        logger.debug("GlueConnection _init_session called")
        logger.debug("GlueConnection session_id : " + self.session_id)
        statement = GlueStatement(client=self.client, session_id=self.session_id, code=SQLPROXY)
        statement.execute()
        statement = GlueStatement(client=self.client, session_id=self.session_id,
                                  code=f"spark.sql('use {self.credentials.database}')")
        statement.execute()

    @property
    def session_id(self):
        if not self._session:
            return None
        return self._session.get("Session", {}).get("Id", None)

    @property
    def client(self):
        if not self._client:
            self._client = boto3.client("glue", region_name=self.credentials.region)
        return self._client

    def cancel_statement(self, statement_id):
        logger.debug("GlueConnection cancel_statement called")
        self.client.cancel_statement(
            SessionId=self.session_id,
            Id=statement_id
        )

    def cancel(self):
        logger.debug("GlueConnection cancel called")
        response = self.client.get_statements(SessionId=self.session_id)
        for statement in response["Statements"]:
            if statement["State"] in ["RUNNING"]:
                self.cancel_statement(statement_id=statement["Id"])

    def close(self):
        logger.debug("NotImplemented: close")

    @staticmethod
    def rollback():
        logger.debug("NotImplemented: rollback")

    def cursor(self, as_dict=False) -> GlueCursor:
        logger.debug("GlueConnection cursor called")
        if not as_dict:
            return GlueCursor(connection=self)
        return GlueDictCursor(connection=self)

    def close_session(self):
        if self.credentials.session_id:
            self.client.delete_session(Id=self.credentials.session_id)

    @property
    def state(self):
        if self._state in [GlueSessionState.FAILED, GlueSessionState.READY]:
            return self._state
        try:
            response = self.client.get_session(Id=self.session_id)
            session = response.get("Session", {})
            self._state = session.get("Status")
        except:
            self._state = GlueSessionState.CLOSED
        return self._state


SQLPROXY = """
import json
import base64
class SqlWrapper2:
    i=0
    dfs={}
    @classmethod
    def execute(cls,sql,output=True):
        if ";" in sql:
            response=None
            queries = sql.split(";")
            for q in queries:
                if (len(q)):
                    if q==queries[-1]:
                        response=cls.execute(q,output=True)
                    else:
                        cls.execute(q,output=False)
            return  response
        
        df=spark.sql(sql)
        if not len(df.schema.fields):
            if output:
                print (json.dumps({"type" : "results","sql" : sql,"schema": None,"results": None}))
            else:
                return json.dumps({"type" : "results","sql" : sql,"schema": None,"results": None})
        results=[]
        rowcount = df.count()
        for record in df.rdd.collect():
            d = {}
            for f in df.schema:
                d[f.name] = record[f.name]
            results.append(({"type": "record", "data": d}))
        if output:
            print(json.dumps({"type": "results", "rowcount": rowcount,"results": results,"description": [{"name":f.name, "type":str(f.dataType)} for f in df.schema]}))
        else:
            return json.dumps({"type": "results", "rowcount": rowcount,"results": results,"description": [{"name":f.name, "type":str(f.dataType)} for f in df.schema]})
"""