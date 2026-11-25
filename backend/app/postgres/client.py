import re
import psycopg
from psycopg.rows import dict_row

SCHEMA_SQL="""SELECT c.table_name,c.column_name,c.data_type,c.is_nullable,c.column_default,
obj_description((quote_ident(c.table_schema)||'.'||quote_ident(c.table_name))::regclass,'pg_class') AS table_description,
col_description((quote_ident(c.table_schema)||'.'||quote_ident(c.table_name))::regclass,c.ordinal_position) AS column_description
FROM information_schema.columns c WHERE c.table_schema='public'
ORDER BY c.table_name,c.ordinal_position"""

class PostgresService:
    def __init__(self, host, port, database, user, password):
        self.cfg=dict(host=host,port=port,dbname=database,user=user,password=password,connect_timeout=5)
    def conn(self):
        return psycopg.connect(**self.cfg,row_factory=dict_row)
    def ping(self):
        with self.conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            return cur.fetchone()["ok"]==1
    def _schema(self,cur):
        cur.execute(SCHEMA_SQL); out={}
        for row in cur.fetchall(): out.setdefault(row["table_name"],[]).append(row)
        return out
    def _begin_consistent_snapshot(self,cur):
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute("SELECT txid_current_snapshot()::text AS snapshot_id,transaction_timestamp() AS snapshot_started_at")
        return cur.fetchone()
    def schema(self):
        with self.conn() as c, c.cursor() as cur:
            return self._schema(cur)
    def profile(self,limit=20):
        limit=max(1,min(int(limit),500)); samples={}
        with self.conn() as c, c.cursor() as cur:
            snapshot=self._begin_consistent_snapshot(cur); schema=self._schema(cur)
            for table in schema:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",table): continue
                cur.execute(f'SELECT * FROM "{table}" LIMIT %s',(limit,))
                samples[table]=cur.fetchall()
        return {
            "database":self.cfg["dbname"],"tables":schema,"samples":samples,
            "snapshot_mode":"repeatable_read_read_only","snapshot_consistent":True,
            "snapshot_id":snapshot["snapshot_id"],"snapshot_started_at":snapshot["snapshot_started_at"],
        }
    def validation_profile(self,max_rows_per_table=10000):
        max_rows_per_table=max(1,min(int(max_rows_per_table),100000))
        samples={}; row_counts={}; truncated_tables=[]
        with self.conn() as c, c.cursor() as cur:
            snapshot=self._begin_consistent_snapshot(cur); schema=self._schema(cur)
            for table in schema:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",table): continue
                cur.execute(f'SELECT count(*) AS count FROM "{table}"')
                row_counts[table]=cur.fetchone()["count"]
                cur.execute(f'SELECT * FROM "{table}" LIMIT %s',(max_rows_per_table+1,))
                rows=cur.fetchall()
                if len(rows)>max_rows_per_table:
                    truncated_tables.append(table); rows=rows[:max_rows_per_table]
                samples[table]=rows
        return {
            "database":self.cfg["dbname"], "tables":schema, "samples":samples,
            "row_counts":row_counts, "truncated_tables":sorted(truncated_tables),
            "max_rows_per_table":max_rows_per_table,
            "snapshot_mode":"repeatable_read_read_only","snapshot_consistent":True,
            "snapshot_id":snapshot["snapshot_id"],"snapshot_started_at":snapshot["snapshot_started_at"],
        }
    def table_data(self,table,limit=100,offset=0):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",table): raise ValueError("非法数据表名称")
        limit=max(1,min(int(limit),500)); offset=max(0,int(offset))
        with self.conn() as c, c.cursor() as cur:
            snapshot=self._begin_consistent_snapshot(cur); schema=self._schema(cur)
            if table not in schema: raise ValueError(f"数据表不存在：{table}")
            cur.execute(f'SELECT count(*) AS count FROM "{table}"')
            total=cur.fetchone()["count"]
            cur.execute(f'SELECT * FROM "{table}" LIMIT %s OFFSET %s',(limit,offset))
            rows=cur.fetchall()
        return {
            "table":table,"total":total,"limit":limit,"offset":offset,"rows":rows,
            "snapshot_mode":"repeatable_read_read_only","snapshot_consistent":True,
            "snapshot_id":snapshot["snapshot_id"],"snapshot_started_at":snapshot["snapshot_started_at"],
        }
