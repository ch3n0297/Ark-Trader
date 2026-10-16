"""launchd 排程產生的純邏輯測試（不碰真的 launchctl，任何平台可跑）"""
import glob
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest

import schedule

HOME = "/Users/someone"
UV = "/opt/homebrew/bin/uv"
PLUGIN = HOME + "/.ark-toolkit/plugin"


def template(job):
    with open(os.path.join(schedule.TEMPLATES, f"{job}.plist.in"), encoding="utf-8") as f:
        return f.read()


def rendered(job, backend=schedule.SIMULATION, extra=None, home=HOME):
    values = schedule.values_for(job, home, UV, PLUGIN, backend,
                                 schedule.build_path(home, UV))
    return plistlib.loads(schedule.render(template(job), values, extra).encode())


class TestTemplates(unittest.TestCase):
    def test_範本不含任何個人路徑或個人label(self):
        for path in glob.glob(os.path.join(schedule.TEMPLATES, "*")):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("/Users/", text, path)
            self.assertNotIn("/opt/homebrew", text, path)
            self.assertNotRegex(text, r"<string>com\.", path)

    def test_每個排程都有範本(self):
        for job in schedule.JOBS:
            self.assertTrue(os.path.isfile(os.path.join(schedule.TEMPLATES, f"{job}.plist.in")))

    def test_產生的plist以uv為進入點並指向plugin連結(self):
        for job in schedule.JOBS:
            p = rendered(job)
            self.assertEqual(p["Label"], "local.ark-trader." + job)
            self.assertEqual(p["ProgramArguments"],
                             [UV, "run", PLUGIN + "/skills/ark-agent/daily.py", job])
            self.assertEqual(p["WorkingDirectory"], HOME + "/.ark-toolkit")
            self.assertEqual(p["EnvironmentVariables"]["HOME"], HOME)
            self.assertEqual(p["StandardOutPath"], f"{HOME}/.ark-toolkit/agent/launchd.{job}.out")

    def test_後端預設simulation_live要明確指定(self):
        for job in ("decide", "settle", "check"):
            self.assertEqual(rendered(job)["EnvironmentVariables"]["ARK_BACKEND"], "simulation")
            self.assertEqual(rendered(job, schedule.LIVE)["EnvironmentVariables"]["ARK_BACKEND"],
                             "live")
        self.assertNotIn("ARK_BACKEND", rendered("review")["EnvironmentVariables"])
        with self.assertRaises(ValueError):
            schedule.values_for("decide", HOME, UV, PLUGIN, "prod", "/bin")

    def test_時段(self):
        decide = rendered("decide")["StartCalendarInterval"]
        self.assertEqual(sorted(d["Weekday"] for d in decide), [1, 2, 3, 4, 5])
        self.assertTrue(all((d["Hour"], d["Minute"]) == (10, 0) for d in decide))
        settle = rendered("settle")["StartCalendarInterval"]
        self.assertTrue(all((d["Hour"], d["Minute"]) == (14, 30) for d in settle))
        self.assertEqual(rendered("review")["StartCalendarInterval"],
                         [{"Weekday": 5, "Hour": 15, "Minute": 0}])
        check = rendered("check")
        self.assertNotIn("StartCalendarInterval", check)
        self.assertTrue(check["RunAtLoad"])
        self.assertNotIn("RunAtLoad", rendered("decide"))     # 10:00 的判斷不補跑

    def test_agent_env寫入且值會跳脫(self):
        env = rendered("decide", extra={"ARK_RULES": "/a & b/<rules>.md",
                                        "ARK_DECISION_MODEL": "opus"})["EnvironmentVariables"]
        self.assertEqual(env["ARK_RULES"], "/a & b/<rules>.md")
        self.assertEqual(env["ARK_DECISION_MODEL"], "opus")

    def test_HOME含特殊字元仍是合法plist(self):
        p = rendered("settle", home="/Users/a&b")
        self.assertEqual(p["EnvironmentVariables"]["HOME"], "/Users/a&b")

    def test_缺值的佔位符直接報錯(self):
        with self.assertRaises(KeyError):
            schedule.render(template("decide"), {"LABEL": "x"})


class TestPathAndEnv(unittest.TestCase):
    def test_PATH以uv與claude目錄優先且去重(self):
        path = schedule.build_path(HOME, "/opt/homebrew/bin/uv", HOME + "/.local/bin/claude")
        parts = path.split(":")
        self.assertEqual(parts[:2], ["/opt/homebrew/bin", HOME + "/.local/bin"])
        self.assertEqual(len(parts), len(set(parts)))
        self.assertIn("/usr/bin", parts)

    def test_env0解析(self):
        self.assertEqual(schedule.parse_env0(b"A=1\0B=x=y\0\0"), {"A": "1", "B": "x=y"})

    def test_env差異剔除保留鍵(self):
        before = {"HOME": "/h", "PATH": "/bin", "SHLVL": "1"}
        after = {"HOME": "/h", "PATH": "/x:/bin", "SHLVL": "2", "_": "/bin/env",
                 "ARK_BACKEND": "live", "ARK_RULES": "/h/r.md", "BASH_FUNC_f%%": "() {}"}
        self.assertEqual(schedule.env_overrides(before, after), {"ARK_RULES": "/h/r.md"})

    @unittest.skipUnless(os.path.exists("/bin/bash"), "需要 bash")
    def test_agent_env以source語意解讀(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        os.makedirs(os.path.join(home, ".ark-toolkit"))
        with open(os.path.join(home, ".ark-toolkit", "agent.env"), "w") as f:
            f.write('# 註解\nexport ARK_RULES="$HOME/docs/rules.md"\n'
                    "ARK_DECISION_MODEL='opus'\nARK_BACKEND=live\n")
        self.assertEqual(schedule.load_agent_env(home),
                         {"ARK_RULES": home + "/docs/rules.md", "ARK_DECISION_MODEL": "opus"})

    def test_沒有agent_env就是空的(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        self.assertEqual(schedule.load_agent_env(home), {})


class FakeLaunchctl:
    def __init__(self, fail_bootstrap=False):
        self.calls = []
        self.fail_bootstrap = fail_bootstrap

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "bootstrap" and self.fail_bootstrap:
            return 5, "Bootstrap failed: 5: Input/output error"
        return (3, "No such process") if args[0] == "bootout" else (0, "")


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        self.plugin = os.path.join(self.home, "plugin")
        os.makedirs(os.path.join(self.plugin, "skills", "ark-agent"))
        open(os.path.join(self.plugin, "skills", "ark-agent", "daily.py"), "w").close()
        self.agents = os.path.join(self.home, "Library", "LaunchAgents")
        self.out = []

    def install(self, launchctl, jobs=schedule.JOBS, **kw):
        return schedule.install(list(jobs), schedule.SIMULATION, self.home, UV, self.plugin,
                                uid=501, launchctl=launchctl, out=self.out.append, **kw)

    def test_寫檔後依序bootout_enable_bootstrap(self):
        fake = FakeLaunchctl()
        self.assertEqual(self.install(fake), 0)
        for job in schedule.JOBS:
            dest = os.path.join(self.agents, f"local.ark-trader.{job}.plist")
            with open(dest, "rb") as f:
                self.assertEqual(plistlib.load(f)["Label"], f"local.ark-trader.{job}")
        self.assertEqual(fake.calls[:3], [
            ("bootout", "gui/501/local.ark-trader.decide"),
            ("enable", "gui/501/local.ark-trader.decide"),
            ("bootstrap", "gui/501", os.path.join(self.agents, "local.ark-trader.decide.plist")),
        ])
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".ark-toolkit", "agent")))

    def test_dry_run不寫檔不呼叫launchctl(self):
        fake = FakeLaunchctl()
        self.assertEqual(self.install(fake, dry_run=True), 0)
        self.assertEqual(fake.calls, [])
        self.assertFalse(os.path.exists(self.agents))
        self.assertIn("local.ark-trader.decide", "\n".join(self.out))

    def test_載入失敗回非零(self):
        self.assertEqual(self.install(FakeLaunchctl(fail_bootstrap=True), jobs=["decide"]), 1)

    def test_有舊排程就拒絕並給移除指令(self):
        os.makedirs(self.agents)
        old = os.path.join(self.agents, "com.example.ark-agent.decide.plist")
        with open(old, "wb") as f:
            plistlib.dump({"Label": "com.example.ark-agent.decide"}, f)
        fake = FakeLaunchctl()
        self.assertEqual(self.install(fake), 2)
        self.assertEqual(fake.calls, [])
        self.assertIn("launchctl bootout gui/501/com.example.ark-agent.decide", "\n".join(self.out))

    def test_plugin連結失效就停(self):
        os.remove(os.path.join(self.plugin, "skills", "ark-agent", "daily.py"))
        self.assertEqual(self.install(FakeLaunchctl()), 1)

    def test_移除排程(self):
        self.install(FakeLaunchctl())
        fake = FakeLaunchctl()
        self.assertEqual(schedule.uninstall(self.home, uid=501, launchctl=fake,
                                            out=self.out.append), 0)
        self.assertEqual(os.listdir(self.agents), [])
        self.assertEqual([c[0] for c in fake.calls], ["bootout"] * len(schedule.JOBS))


class TestInstallScript(unittest.TestCase):
    SCRIPT = os.path.abspath(os.path.join(schedule.HERE, "..", "..", "install.sh"))

    @unittest.skipUnless(os.path.exists("/bin/bash"), "需要 bash")
    def test_安裝腳本語法正確(self):
        subprocess.run(["/bin/bash", "-n", self.SCRIPT], check=True)

    def test_安裝腳本可執行(self):
        self.assertTrue(os.access(self.SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
