import binascii
import contextlib
import time

from nxc.helpers.misc import gen_random_string


class MSSQLEXEC:
    def __init__(self, connection, logger):
        self.mssql_conn = connection
        self.logger = logger

        # Store the original state of options that have to be enabled/disabled in order to restore them later
        self.backuped_options = {}

    def execute(self, command):
        result = ""

        self.backup_and_enable("advanced options")
        self.backup_and_enable("xp_cmdshell")

        try:
            cmd = f"exec master..xp_cmdshell '{command}'"
            self.logger.debug(f"Attempting to execute query: {cmd}")
            raw = self.mssql_conn.sql_query(cmd)
            self.logger.debug(f"Raw results from query: {raw}")
            if raw:
                result = "\n".join(line["output"] for line in raw if line["output"] != "NULL")
                self.logger.debug(f"Concatenated result together for easier parsing: {result}")
                # if you prepend SilentlyContinue it will still output the error, but it will still continue on (so it's not silent...)
                if "Preparing modules for first use" in result and "Completed" not in result:
                    self.logger.error("Error when executing PowerShell (received 'preparing modules for first use'), try prepending $ProgressPreference = 'SilentlyContinue'; to your command")
        except Exception as e:
            self.logger.error(f"Error when attempting to execute command via xp_cmdshell: {e}")

        self.restore("xp_cmdshell")
        self.restore("advanced options")

        return result

    def restore(self, option):
        try:
            if not self.backuped_options[option]:
                self.logger.debug(f"Option '{option}' was not enabled originally, attempting to disable it.")
                query = f"EXEC master.dbo.sp_configure '{option}', 0;RECONFIGURE;"
                self.logger.debug(f"Executing query: {query}")
                self.mssql_conn.sql_query(query)
            else:
                self.logger.debug(f"Option '{option}' was originally enabled, leaving it enabled.")
        except Exception as e:
            self.logger.error(f"[OPSEC] Error when attempting to restore option '{option}': {e}")

    def backup_and_enable(self, option):
        try:
            self.backuped_options[option] = self.is_option_enabled(option)
            if not self.backuped_options[option]:
                self.logger.debug(f"Option '{option}' is disabled, attempting to enable it.")
                query = f"EXEC master.dbo.sp_configure '{option}', 1;RECONFIGURE;"
                self.logger.debug(f"Executing query: {query}")
                self.mssql_conn.sql_query(query)
            else:
                self.logger.debug(f"Option '{option}' is already enabled.")
        except Exception as e:
            self.logger.error(f"Error when checking/enabling option '{option}': {e}")

    def is_option_enabled(self, option):
        query = f"EXEC master.dbo.sp_configure '{option}';"
        self.logger.debug(f"Checking if {option} is enabled: {query}")
        result = self.mssql_conn.sql_query(query)
        # Assuming the query returns a list of dictionaries with 'config_value' as the key
        self.logger.debug(f"{option} check result: {result}")
        return bool(result and result[0]["config_value"] == 1)

    def execute_agent_job(self, command):
        """Execute command via SQL Server Agent Job (CmdExec subsystem).
        Runs as the SQL Server Agent service account (typically NT Service\\SQLSERVERAGENT).
        Requires SQL Server Agent service to be running.
        """
        result = ""
        job_name = f"nxc_{gen_random_string(6)}"
        step_name = gen_random_string(6)
        tmp_file = f"C:\\Windows\\Temp\\{gen_random_string(8)}.txt"

        self.backup_and_enable("advanced options")
        self.backup_and_enable("xp_cmdshell")

        try:
            escaped_cmd = command.replace("'", "''")

            for query in [
                f"USE msdb; EXEC sp_add_job @job_name = N'{job_name}';",
                f"USE msdb; EXEC sp_add_jobstep @job_name = N'{job_name}', @step_name = N'{step_name}', @subsystem = N'CmdExec', @command = N'cmd /c \"{escaped_cmd}\" > {tmp_file} 2>&1', @retry_attempts = 0;",
                f"USE msdb; EXEC sp_add_jobserver @job_name = N'{job_name}';",
                f"USE msdb; EXEC sp_start_job @job_name = N'{job_name}';",
            ]:
                self.logger.debug(f"Agent job query: {query}")
                self.mssql_conn.sql_query(query)

            # Poll for completion — status 4 = Idle (done), 1 = Executing
            completed = False
            time.sleep(1)
            for _ in range(30):
                status = self.mssql_conn.sql_query(f"EXEC msdb.dbo.sp_help_job @job_name = N'{job_name}', @job_aspect = N'JOB'")
                self.logger.debug(f"Agent job status: {status}")
                if status and status[0].get("current_execution_status") == 4:
                    completed = True
                    break
                time.sleep(1)

            if not completed:
                self.logger.error("Agent job timed out after 30 seconds")
            else:
                raw = self.mssql_conn.sql_query(f"EXEC xp_cmdshell 'type \"{tmp_file}\"'")
                if raw:
                    result = "\n".join(line["output"] for line in raw if line.get("output") and line["output"] != "NULL")

        except Exception as e:
            self.logger.error(f"Error executing via agent job: {e}")
        finally:
            with contextlib.suppress(Exception):
                self.mssql_conn.sql_query(f"USE msdb; EXEC sp_delete_job @job_name = N'{job_name}', @delete_unused_schedule = 1;")
            with contextlib.suppress(Exception):
                self.mssql_conn.sql_query(f"EXEC xp_cmdshell 'del \"{tmp_file}\"'")
            self.restore("xp_cmdshell")
            self.restore("advanced options")

        return result

    def execute_ole(self, command):
        """Execute command via OLE Automation (wscript.shell).
        Output is captured by writing to a temp file read back via ADODB.Stream.
        Does not use xp_cmdshell.
        """
        result = ""
        tmp_file = f"C:\\Windows\\Temp\\{gen_random_string(8)}.txt"

        self.backup_and_enable("advanced options")
        self.backup_and_enable("Ole Automation Procedures")

        try:
            escaped_cmd = command.replace("'", "''")

            run_q = f"""
                DECLARE @objShell INT;
                EXEC sp_OACreate 'wscript.shell', @objShell OUTPUT;
                EXEC sp_OAMethod @objShell, 'run', NULL, 'cmd.exe /c "{escaped_cmd}" > {tmp_file} 2>&1', 0, 1;
                EXEC sp_OADestroy @objShell;
            """
            self.logger.debug(f"OLE run query: {run_q}")
            self.mssql_conn.sql_query(run_q)

            read_q = f"""
                DECLARE @objStream INT;
                DECLARE @fileContent NVARCHAR(MAX);
                EXEC sp_OACreate 'ADODB.Stream', @objStream OUTPUT;
                EXEC sp_OASetProperty @objStream, 'Type', 2;
                EXEC sp_OASetProperty @objStream, 'Charset', 'utf-8';
                EXEC sp_OAMethod @objStream, 'Open';
                EXEC sp_OAMethod @objStream, 'LoadFromFile', NULL, '{tmp_file}';
                EXEC sp_OAMethod @objStream, 'ReadText', @fileContent OUTPUT;
                EXEC sp_OAMethod @objStream, 'Close';
                EXEC sp_OADestroy @objStream;
                SELECT @fileContent AS output;
            """
            self.logger.debug(f"OLE read query: {read_q}")
            raw = self.mssql_conn.sql_query(read_q)
            if raw and raw[0].get("output"):
                result = raw[0]["output"].strip()

        except Exception as e:
            self.logger.error(f"Error executing via OLE automation: {e}")
        finally:
            with contextlib.suppress(Exception):
                cleanup_q = f"""
                    DECLARE @objShellClean INT;
                    EXEC sp_OACreate 'wscript.shell', @objShellClean OUTPUT;
                    EXEC sp_OAMethod @objShellClean, 'run', NULL, 'cmd.exe /c del {tmp_file}', 0, 1;
                    EXEC sp_OADestroy @objShellClean;
                """
                self.mssql_conn.sql_query(cleanup_q)
            self.restore("Ole Automation Procedures")
            self.restore("advanced options")

        return result

    def put_file(self, data, remote):
        try:
            self.backup_and_enable("advanced options")
            self.backup_and_enable("Ole Automation Procedures")
            hexdata = data.hex()
            self.logger.debug(f"Hex data to write to file: {hexdata}")
            query = f"DECLARE @ob INT;EXEC sp_OACreate 'ADODB.Stream', @ob OUTPUT;EXEC sp_OASetProperty @ob, 'Type', 1;EXEC sp_OAMethod @ob, 'Open';EXEC sp_OAMethod @ob, 'Write', NULL, 0x{hexdata};EXEC sp_OAMethod @ob, 'SaveToFile', NULL, '{remote}', 2;EXEC sp_OAMethod @ob, 'Close';EXEC sp_OADestroy @ob;"
            self.logger.debug(f"Executing query: {query}")
            self.mssql_conn.sql_query(query)
            self.restore("Ole Automation Procedures")
            self.restore("advanced options")
        except Exception as e:
            self.logger.debug(f"Error uploading via mssqlexec: {e}")

    def file_exists(self, remote):
        try:
            query = f"DECLARE @r INT; EXEC master.dbo.xp_fileexist '{remote}', @r OUTPUT; SELECT @r as n"
            self.logger.debug(f"Executing query: {query}")
            res = self.mssql_conn.batch(query)
            self.logger.debug(f"File check response: {res}")
            return res[0]["n"] == 1
        except Exception:
            return False

    def get_file(self, remote, local):
        try:
            query = f"SELECT * FROM OPENROWSET(BULK N'{remote}', SINGLE_BLOB) rs"
            self.logger.debug(f"Executing query: {query}")
            self.mssql_conn.sql_query(query)
            data = self.mssql_conn.rows
            self.logger.debug(f"Get file returned: {data}")
            with open(local, "wb+") as f:
                f.write(binascii.unhexlify(data[0]["BulkColumn"]))
        except Exception as e:
            self.logger.debug(f"Error downloading via mssqlexec: {e}")
