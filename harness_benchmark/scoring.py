"""Keep missing grading distinct from an executed official failure."""
import re


def harbor_grade(dataset, reward, evidence, stdout=''):
    if reward not in [0, 1]:
        return None
    if dataset == 'swebench-verified':
        status = evidence.get('tests_status')
        if not isinstance(evidence.get('resolved'), bool) or not isinstance(status, dict):
            return None
        count = sum(len(values) for group in status.values() if isinstance(group, dict)
            for values in group.values() if isinstance(values, list))
        if not count or float(evidence['resolved']) != reward:
            return None
        if re.search(r'requires pytest-[^\n]+actual pytest-0\.1|No module named swebench|Failed to download.*(?:python|swebench)', stdout):
            return None
        return float(reward)
    if dataset == 'terminal-bench':
        summary = evidence.get('results', {}).get('summary', {})
        tests = evidence.get('results', {}).get('tests', [])
        if summary.get('tests', 0) <= 0 or summary.get('passed', 0)+summary.get('failed', 0) <= 0:
            return None
        if reward == 1 and summary.get('passed') != summary.get('tests'):
            return None
        if reward == 0 and summary.get('passed') == summary.get('tests'):
            return None
        # Dependency/import errors during setup do not constitute a valid task 0.
        messages = '\n'.join(str(t.get('message', '')) for t in tests)
        if re.search(r'FileNotFoundError[^\n]*(?:sshpass|pytest)|ModuleNotFoundError[^\n]*(?:pytest|swebench)', messages):
            return None
        return float(reward)
    raise ValueError('Unsupported Harbor grading dataset')
