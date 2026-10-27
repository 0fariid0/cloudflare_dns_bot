"""Exercise installer failure handling in temporary directories with fake services/git."""
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class Installer(unittest.TestCase):
    def run_scenario(self,body):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            manager=root/'manager.sh'
            source=(ROOT/'setup.sh').read_text()
            self.assertTrue(source.endswith('main "$@"\n'))
            manager.write_text(source.removesuffix('main "$@"\n'))
            old=root/'app';old.mkdir()
            (old/'.git').mkdir()
            (old/'bot.py').write_text('old')
            (old/'config.py').write_text('secret configuration')
            (old/'users.json').write_text('{"before": true}')
            with sqlite3.connect(old/'dns_advanced.sqlite3') as c:
                c.execute('CREATE TABLE preserved(value TEXT)')
                c.execute("INSERT INTO preserved VALUES('groups-and-clones')")
            incoming=root/'incoming';incoming.mkdir()
            for name in ('bot.py','install.sh','requirements.txt'):(incoming/name).write_text('new')
            script='''set -Eeuo pipefail
source "$1/manager.sh"
INSTALL_DIR="$1/app"
BACKUP_ROOT="$1/backups"
SCENARIO_ROOT="$1"
install_bootstrap_dependencies() { :; }
pause() { :; }
info() { :; }
success() { :; }
warn() { :; }
systemctl() {
  printf '%s\\n' "$*" >> "$SCENARIO_ROOT/services"
  return 0
}
run_installer() { :; }
git() {
  if [[ "$1" == clone ]]; then
    cp -a "$SCENARIO_ROOT/incoming/." "${@: -1}/"
  elif [[ "$1" == rev-parse ]]; then
    echo old-sha
  fi
}
''' +body
            result=subprocess.run(['bash','-c',script,'test',str(root)],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr+'\n'+result.stdout)

    def test_clone_failure_keeps_original_service_and_source(self):
        self.run_scenario('''
git() { return 1; }
if install_bot; then exit 70; fi
[[ "$(cat "$INSTALL_DIR/bot.py")" == old ]]
[[ ! -e "$SCENARIO_ROOT/services" ]]
''')

    def test_successful_install_preserves_config_users_and_sqlite(self):
        self.run_scenario('''
install_bot
[[ "$(cat "$INSTALL_DIR/bot.py")" == new ]]
[[ "$(cat "$INSTALL_DIR/config.py")" == 'secret configuration' ]]
python3 - "$INSTALL_DIR/dns_advanced.sqlite3" <<'SQL'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as c:
    assert c.execute('SELECT value FROM preserved').fetchone()[0]=='groups-and-clones'
SQL
[[ -f "$INSTALL_DIR/users.json" ]]
''')

    def test_install_failure_restores_previous_source(self):
        self.run_scenario('''
run_installer() { [[ "$(cat "$INSTALL_DIR/bot.py")" == old ]]; }
if install_bot; then exit 70; fi
[[ "$(cat "$INSTALL_DIR/bot.py")" == old ]]
[[ "$(cat "$INSTALL_DIR/config.py")" == 'secret configuration' ]]
[[ -f "$INSTALL_DIR/dns_advanced.sqlite3" ]]
''')

    def test_update_fetch_failure_does_not_stop_service(self):
        self.run_scenario('''
git() {
  if [[ "$1" == rev-parse ]]; then echo old-sha; return; fi
  return 1
}
if update_bot; then exit 70; fi
[[ ! -e "$SCENARIO_ROOT/services" ]]
[[ "$(cat "$INSTALL_DIR/bot.py")" == old ]]
''')

    def test_update_failure_rolls_back_code_and_uses_latest_runtime_snapshot(self):
        self.run_scenario('''
systemctl() {
  if [[ "$1" == stop ]]; then printf '{"latest": true}\\n' > "$INSTALL_DIR/users.json"; fi
  return 0
}
git() {
  if [[ "$1" == rev-parse ]]; then echo old-sha
  elif [[ "$1" == reset && "$3" == origin/main ]]; then
    echo new > "$INSTALL_DIR/bot.py"
    echo 'overwritten tracked data' > "$INSTALL_DIR/users.json"
  elif [[ "$1" == reset ]]; then echo old > "$INSTALL_DIR/bot.py"
  fi
  return 0
}
run_installer() { [[ "$(cat "$INSTALL_DIR/bot.py")" == old ]]; }
if update_bot; then exit 70; fi
[[ "$(cat "$INSTALL_DIR/bot.py")" == old ]]
[[ "$(cat "$INSTALL_DIR/users.json")" == '{"latest": true}' ]]
''')


if __name__=='__main__':unittest.main()
