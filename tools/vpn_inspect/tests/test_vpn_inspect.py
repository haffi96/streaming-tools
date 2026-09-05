import subprocess
import unittest
from unittest.mock import patch

from vpn_inspect.inspect import interface_for_address, run_command


class InterfaceTests(unittest.TestCase):
    def test_finds_interface_by_ipv4_address(self) -> None:
        interfaces = [
            {"name": "en0", "ipv4": ["192.168.1.2"]},
            {"name": "utun4", "ipv4": ["10.20.30.4"]},
        ]

        self.assertEqual(interface_for_address(interfaces, "10.20.30.4"), "utun4")
        self.assertIsNone(interface_for_address(interfaces, "10.20.30.5"))


class CommandTests(unittest.TestCase):
    @patch("vpn_inspect.inspect.shutil.which", return_value=None)
    def test_reports_missing_command(self, _which: object) -> None:
        result = run_command(["missing", "argument"], 1)

        self.assertFalse(result["available"])
        self.assertFalse(result["success"])

    @patch("vpn_inspect.inspect.shutil.which", return_value="/usr/bin/tool")
    @patch("vpn_inspect.inspect.subprocess.run")
    def test_reports_success(self, run: object, _which: object) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["tool"], returncode=0, stdout="output\n", stderr=""
        )

        result = run_command(["tool"], 1)

        self.assertTrue(result["success"])
        self.assertEqual(result["stdout"], "output")
