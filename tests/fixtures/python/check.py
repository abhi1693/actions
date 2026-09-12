import os
import unittest
from pathlib import Path


class EnvironmentTest(unittest.TestCase):
    def test_application_environment(self):
        self.assertEqual(os.environ["CI_FIXTURE"], "python")
        if "CI_DATABASE_URL" in os.environ:
            self.assertTrue(
                os.environ["CI_DATABASE_URL"].startswith(
                    "postgresql://ci:ci@127.0.0.1:"
                )
            )


if __name__ == "__main__":
    result = unittest.TextTestRunner().run(
        unittest.defaultTestLoader.loadTestsFromTestCase(EnvironmentTest)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
    Path("reports").mkdir(exist_ok=True)
    Path("reports/junit.xml").write_text(
        '<testsuites><testsuite tests="1" failures="0"><testcase name="environment"/></testsuite></testsuites>'
    )
