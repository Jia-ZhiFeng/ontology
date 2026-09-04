import threading
from uuid import uuid4

from ..core.security import mask_payload


class LLMConfigurationError(RuntimeError):
    pass


class LLMConfigurationNotFound(LLMConfigurationError):
    pass


class LLMConfigurationConflict(LLMConfigurationError):
    pass


class LLMConfigurationEncryptionError(LLMConfigurationError):
    pass


class PostgresLLMConfigurationRepository:
    """Persist managed LLM profiles while keeping API keys encrypted at rest.

    PostgreSQL pgcrypto performs authenticated OpenPGP symmetric encryption.
    The passphrase is supplied as a bind parameter and is never stored in the
    configuration table or returned by summary methods.
    """

    _schema_lock=threading.Lock()
    _schema_ready=False
    _management_lock_id=824_731_017

    _SUMMARY_COLUMNS="""
        id,name,provider,base_url,model,temperature,api_version,
        verification_status AS status,is_active,
        (api_key_ciphertext IS NOT NULL) AS api_key_configured,
        version,tested_version,last_tested_at,last_error,created_at,updated_at
    """

    def __init__(self,postgres_service,encryption_key:str):
        self.postgres=postgres_service
        self.encryption_key=(encryption_key or "").strip()

    def _key(self)->str:
        if not self.encryption_key:
            raise LLMConfigurationEncryptionError(
                "服务端未配置 LLM 配置加密密钥"
            )
        return self.encryption_key

    def ensure_schema(self):
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            with self.postgres.conn() as connection,connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
                cursor.execute("CREATE SCHEMA IF NOT EXISTS mvp_control")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS mvp_control.llm_configurations (
                        id uuid PRIMARY KEY,
                        name varchar(80) NOT NULL,
                        provider varchar(32) NOT NULL CHECK (provider IN (
                            'openai_compatible','deepseek','aliyun_bailian',
                            'azure_openai','custom'
                        )),
                        base_url text NOT NULL,
                        model varchar(160) NOT NULL,
                        temperature double precision NOT NULL DEFAULT 0
                            CHECK (temperature >= 0 AND temperature <= 2),
                        api_version varchar(32),
                        api_key_ciphertext bytea NOT NULL,
                        verification_status varchar(16) NOT NULL DEFAULT 'unverified'
                            CHECK (verification_status IN ('unverified','verified','failed')),
                        is_active boolean NOT NULL DEFAULT false,
                        version integer NOT NULL DEFAULT 1 CHECK (version > 0),
                        tested_version integer,
                        last_tested_at timestamptz,
                        last_error varchar(500),
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now()
                    )
                """)
                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS llm_configurations_single_active
                    ON mvp_control.llm_configurations ((is_active)) WHERE is_active
                """)
            type(self)._schema_ready=True

    @staticmethod
    def _summary(row):
        if not row:
            return None
        public_fields=(
            "id","name","provider","base_url","model","temperature",
            "api_version","status","is_active","api_key_configured",
            "version","tested_version","last_tested_at","last_error",
            "created_at","updated_at",
        )
        output={key:row.get(key) for key in public_fields}
        output["id"]=str(output["id"])
        configured=bool(output.pop("api_key_configured",False))
        output["api_key_configured"]=configured
        output["api_key_masked"]="••••••••" if configured else ""
        if output.get("last_error"):
            output["last_error"]=str(mask_payload(output["last_error"]))[:500]
        return output

    def list_configs(self)->list[dict]:
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute(f"""
                SELECT {self._SUMMARY_COLUMNS}
                FROM mvp_control.llm_configurations
                ORDER BY is_active DESC,updated_at DESC,name,id
            """)
            return [self._summary(row) for row in cursor.fetchall()]

    def get_summary(self,configuration_id:str)->dict:
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute(f"""
                SELECT {self._SUMMARY_COLUMNS}
                FROM mvp_control.llm_configurations WHERE id=%s
            """,(configuration_id,))
            row=cursor.fetchone()
        if not row:
            raise LLMConfigurationNotFound("模型配置不存在")
        return self._summary(row)

    def active_metadata(self)->dict|None:
        """Return active metadata without decrypting or selecting the secret."""
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute(f"""
                SELECT {self._SUMMARY_COLUMNS}
                FROM mvp_control.llm_configurations WHERE is_active
            """)
            row=cursor.fetchone()
        return self._summary(row)

    def get_runtime(self,configuration_id:str)->dict:
        self.ensure_schema()
        try:
            with self.postgres.conn() as connection,connection.cursor() as cursor:
                cursor.execute("""
                    SELECT id,name,provider,base_url,model,temperature,api_version,
                           version,tested_version,verification_status,is_active,
                           pgp_sym_decrypt(api_key_ciphertext,%s) AS api_key
                    FROM mvp_control.llm_configurations WHERE id=%s
                """,(self._key(),configuration_id))
                row=cursor.fetchone()
        except Exception as exc:
            raise LLMConfigurationEncryptionError(
                "模型密钥无法解密，请检查服务端加密密钥"
            ) from exc
        if not row:
            raise LLMConfigurationNotFound("模型配置不存在")
        output=dict(row)
        output["id"]=str(output["id"])
        return output

    def create(
        self,*,name:str,provider:str,base_url:str,model:str,
        temperature:float,api_key:str,api_version:str|None=None,
    )->dict:
        self.ensure_schema()
        configuration_id=str(uuid4())
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",(self._management_lock_id,)
            )
            cursor.execute("""
                INSERT INTO mvp_control.llm_configurations (
                    id,name,provider,base_url,model,temperature,api_version,
                    api_key_ciphertext
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,
                    pgp_sym_encrypt(%s,%s,'cipher-algo=aes256,compress-algo=0')
                )
            """,(
                configuration_id,name,provider,base_url,model,temperature,
                api_version,api_key,self._key(),
            ))
        return self.get_summary(configuration_id)

    def update(
        self,configuration_id:str,*,name:str,provider:str,base_url:str,
        model:str,temperature:float,api_version:str|None=None,
        api_key:str|None=None,expected_version:int,
    )->dict:
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",(self._management_lock_id,)
            )
            cursor.execute("""
                SELECT name,provider,base_url,model,temperature,api_version,version
                FROM mvp_control.llm_configurations WHERE id=%s FOR UPDATE
            """,(configuration_id,))
            current=cursor.fetchone()
            if not current:
                raise LLMConfigurationNotFound("模型配置不存在")
            if current["version"]!=expected_version:
                raise LLMConfigurationConflict(
                    "模型配置已被其他操作修改，请刷新后重试"
                )
            connection_changed=bool(api_key) or any((
                current["provider"]!=provider,
                current["base_url"]!=base_url,
                current["model"]!=model,
                float(current["temperature"])!=float(temperature),
                (current["api_version"] or None)!=(api_version or None),
            ))
            secret_sql=""
            params=[name,provider,base_url,model,temperature,api_version]
            if api_key:
                secret_sql=(
                    ",api_key_ciphertext=pgp_sym_encrypt("
                    "%s,%s,'cipher-algo=aes256,compress-algo=0')"
                )
                params.extend((api_key,self._key()))
            reset_sql=""
            if connection_changed:
                reset_sql="""
                    ,verification_status='unverified',tested_version=NULL,
                     last_tested_at=NULL,last_error=NULL,is_active=false
                """
            else:
                # A display-only rename still advances the row version. Carry a
                # successful test forward so the active/version invariant holds.
                reset_sql="""
                    ,tested_version=CASE
                        WHEN tested_version IS NULL THEN NULL ELSE version+1 END
                """
            params.extend((configuration_id,expected_version))
            cursor.execute(f"""
                UPDATE mvp_control.llm_configurations
                SET name=%s,provider=%s,base_url=%s,model=%s,temperature=%s,
                    api_version=%s{secret_sql}{reset_sql},
                    version=version+1,updated_at=now()
                WHERE id=%s AND version=%s
            """,tuple(params))
            if not cursor.rowcount:
                raise LLMConfigurationConflict(
                    "模型配置已被其他操作修改，请刷新后重试"
                )
        return self.get_summary(configuration_id)

    def mark_test_result(
        self,configuration_id:str,*,tested_version:int,
        success:bool,error:str|None=None,
    )->dict:
        self.ensure_schema()
        status="verified" if success else "failed"
        safe_error=(
            None
            if success
            else str(mask_payload(error or "模型连接测试失败"))[:500]
        )
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute("""
                UPDATE mvp_control.llm_configurations
                SET verification_status=%s,tested_version=%s,
                    last_tested_at=now(),last_error=%s,
                    is_active=CASE WHEN %s THEN is_active ELSE false END,
                    updated_at=now()
                WHERE id=%s AND version=%s
                RETURNING id
            """,(
                status,tested_version,safe_error,success,
                configuration_id,tested_version,
            ))
            updated=cursor.fetchone()
            if not updated:
                cursor.execute(
                    "SELECT 1 AS found FROM mvp_control.llm_configurations WHERE id=%s",
                    (configuration_id,),
                )
                if not cursor.fetchone():
                    raise LLMConfigurationNotFound("模型配置不存在")
                raise LLMConfigurationConflict(
                    "模型配置在测试期间已被修改，请重新测试"
                )
        return self.get_summary(configuration_id)

    def activate(self,configuration_id:str)->dict:
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",(self._management_lock_id,)
            )
            cursor.execute("""
                SELECT verification_status,version,tested_version
                FROM mvp_control.llm_configurations WHERE id=%s FOR UPDATE
            """,(configuration_id,))
            row=cursor.fetchone()
            if not row:
                raise LLMConfigurationNotFound("模型配置不存在")
            if (
                row["verification_status"]!="verified"
                or row["tested_version"]!=row["version"]
            ):
                raise LLMConfigurationConflict(
                    "模型配置必须在当前版本连接测试成功后才能启用"
                )
            cursor.execute(
                "UPDATE mvp_control.llm_configurations SET is_active=false "
                "WHERE is_active AND id<>%s",
                (configuration_id,),
            )
            cursor.execute("""
                UPDATE mvp_control.llm_configurations
                SET is_active=true,updated_at=now() WHERE id=%s
            """,(configuration_id,))
        return self.get_summary(configuration_id)

    def active_runtime(self)->dict|None:
        metadata=self.active_metadata()
        if not metadata:
            return None
        if (
            metadata["status"]!="verified"
            or metadata["tested_version"]!=metadata["version"]
        ):
            raise LLMConfigurationConflict(
                "当前启用模型未通过本版本连接测试"
            )
        try:
            runtime=self.get_runtime(metadata["id"])
        except LLMConfigurationNotFound as exc:
            raise LLMConfigurationConflict(
                "启用模型在读取期间发生变化，请重试"
            ) from exc
        if not runtime["is_active"]:
            raise LLMConfigurationConflict("启用模型在读取期间发生变化，请重试")
        return runtime

    def deactivate(self,configuration_id:str)->dict:
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",(self._management_lock_id,)
            )
            cursor.execute(
                "SELECT id FROM mvp_control.llm_configurations "
                "WHERE id=%s FOR UPDATE",
                (configuration_id,),
            )
            if not cursor.fetchone():
                raise LLMConfigurationNotFound("模型配置不存在")
            cursor.execute("""
                UPDATE mvp_control.llm_configurations
                SET is_active=false,updated_at=now() WHERE id=%s
            """,(configuration_id,))
        return self.get_summary(configuration_id)

    def delete(self,configuration_id:str,*,expected_version:int):
        self.ensure_schema()
        with self.postgres.conn() as connection,connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",(self._management_lock_id,)
            )
            cursor.execute(
                "SELECT is_active,version FROM mvp_control.llm_configurations "
                "WHERE id=%s FOR UPDATE",
                (configuration_id,),
            )
            row=cursor.fetchone()
            if not row:
                raise LLMConfigurationNotFound("模型配置不存在")
            if row["is_active"]:
                raise LLMConfigurationConflict("请先启用其他模型后再删除当前启用配置")
            if row["version"]!=expected_version:
                raise LLMConfigurationConflict(
                    "模型配置已被其他操作修改，请刷新后重试"
                )
            cursor.execute(
                "DELETE FROM mvp_control.llm_configurations "
                "WHERE id=%s AND version=%s",
                (configuration_id,expected_version),
            )
            if not cursor.rowcount:
                raise LLMConfigurationConflict(
                    "模型配置已被其他操作修改，请刷新后重试"
                )
