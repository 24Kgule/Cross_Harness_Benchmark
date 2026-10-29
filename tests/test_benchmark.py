"""Local regression checks. Temporary synthetic records are never benchmark scores."""
import asyncio
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness_benchmark.common import ROOT, save_json
from harness_benchmark.config import load_config, make_plan
from harness_benchmark.prepare import freeze_plan, download_tasks, git_blob
from harness_benchmark.scoring import harbor_grade
from harness_benchmark.metrics import phases, tokens


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = json.loads((ROOT/'configs/quickstart.json').read_text())
        self.cfg['output_dir'] = str(Path(self.temp.name)/'run')

    def load(self, cfg=None):
        path = Path(self.temp.name)/'config.json'
        save_json(path, self.cfg if cfg is None else cfg)
        return load_config(path)

    def test_shared_sample_deterministic_independent_of_harness_subset(self):
        first = make_plan(self.load())
        self.assertEqual(first, make_plan(self.load()))
        self.cfg['harnesses'] = ['kimi']
        second = make_plan(self.load())
        self.assertEqual(first['datasets'], second['datasets'])
        names = {c['task'] for c in first['cells'] if c['harness'] == 'dsh'}
        self.assertEqual(names, {c['task'] for c in first['cells'] if c['harness'] == 'pi'})
        self.assertEqual(len(first['cells']), 4)

    def test_all_and_nested_prefix(self):
        small = make_plan(self.load())
        self.cfg['datasets'][0]['sample_size'] = 'all'
        full = make_plan(self.load())
        self.assertEqual(len(full['datasets'][0]['tasks']), 89)
        self.assertEqual(full['datasets'][0]['tasks'][:2], small['datasets'][0]['tasks'])

    def test_explicit_ids_and_disabled_dataset(self):
        self.cfg['datasets'] = [dict(name='terminal-bench', task_ids=['fix-git']),
                                dict(name='appworld', enabled=False)]
        plan = make_plan(self.load())
        self.assertEqual(len(plan['cells']), 2)
        self.assertEqual(plan['cells'][0]['task'], 'fix-git')

    def test_reject_invalid_configs_before_dispatch(self):
        cases = [dict(harnesses=[]), dict(harnesses=['dsh', 'dsh']), dict(harnesses=[[]]),
                 dict(harnesses=['unknown']), dict(execution=[]), dict(execution={'workers':0}),
                 dict(execution={'min_cpus':2}), dict(output_dir='harness_benchmark'),
                 dict(output_dir=str(ROOT.parent)), dict(unknown=True),
                 dict(datasets=[dict(name='terminal-bench', sample_size=True)]),
                 dict(datasets=[dict(name='terminal-bench', sample_size=[])]),
                 dict(datasets=[dict(name='terminal-bench', sample_size=1, task_ids=['fix-git'])]),
                 dict(datasets=[dict(name='terminal-bench', task_ids=['../tests'])]),
                 dict(datasets=[dict(name='terminal-bench', task_ids=[[]])]),
                 dict(datasets=[dict(name='terminal-bench', enabled=False)]),
                 dict(datasets=[dict(name='terminal-bench', sample_size=90)])]
        for changes in cases:
            with self.subTest(changes=changes):
                cfg = {**self.cfg, **changes}
                with self.assertRaises(ValueError):
                    make_plan(self.load(cfg))

    def test_frozen_plan_refuses_changed_sample(self):
        plan = make_plan(self.load())
        freeze_plan(plan)
        self.assertEqual(freeze_plan(plan), Path(self.cfg['output_dir']))
        self.cfg['datasets'][0]['seed'] += 1
        with self.assertRaises(ValueError):
            freeze_plan(make_plan(self.load()))

    def test_resume_never_reruns_zero_or_missing_grades_and_rejects_orphans(self):
        from harness_benchmark.run import pending_cells
        plan = make_plan(self.load())
        out = freeze_plan(plan)
        first, second = plan['cells'][:2]
        save_json(out/'cells/00000/record.json', {**first, 'official_score':0})
        save_json(out/'cells/00001/record.json', {**second, 'official_score':None})
        self.assertEqual(len(pending_cells(plan, out)), 2)
        save_json(out/'cells/00002/started.json', {'started_at':'synthetic'})
        with self.assertRaises(ValueError):
            pending_cells(plan, out)

    def test_asset_download_validates_blob_and_executable_mode(self):
        data = b'#!/bin/sh\nexit 0\n'
        self.cfg['datasets'] = [dict(name='terminal-bench', task_ids=['fix-git'])]
        plan = make_plan(self.load())
        entry = plan['datasets'][0]['tasks'][0]
        tree = {'tree':[dict(path=entry['path']+'/tests/test.sh', type='blob', mode='100755', sha=git_blob(data))]}
        with patch('harness_benchmark.prepare.fetch', side_effect=[json.dumps(tree).encode(), data]):
            download_tasks(plan)
        target = Path(self.cfg['output_dir'])/'assets/terminal-bench/fix-git/tests/test.sh'
        self.assertEqual(target.read_bytes(), data)
        self.assertTrue(target.stat().st_mode & 0o111)
        # A resumed download must repair changed bytes rather than bless them.
        target.write_bytes(b'bad')
        with patch('harness_benchmark.prepare.fetch', return_value=b'wrong'):
            with self.assertRaises(RuntimeError):
                download_tasks(plan)


class ScoringTests(unittest.TestCase):
    def test_provider_access_or_model_errors_do_not_masquerade_as_task_grades(self):
        from harness_benchmark.run import check_provider_evidence
        record = check_provider_evidence({'official_score':0,'status':'official_scored'},[{'status':402}])
        self.assertIsNone(record['official_score'])
        self.assertEqual(record['observed_grader_score'],0)
        record = check_provider_evidence({'official_score':1},[{'status':200,'response_models':['other'],'model_echo_allowed':False}])
        self.assertIsNone(record['official_score'])

    def test_terminal_failure_is_valid_but_missing_or_conflicting_grading_is_not(self):
        evidence = dict(results=dict(summary=dict(tests=2, passed=1, failed=1), tests=[dict(status='failed')]))
        self.assertEqual(harbor_grade('terminal-bench', 0, evidence), 0)
        self.assertIsNone(harbor_grade('terminal-bench', 1, evidence))
        self.assertIsNone(harbor_grade('terminal-bench', 0, {}))
        evidence['results']['tests'][0]['message'] = 'ModuleNotFoundError: pytest'
        self.assertIsNone(harbor_grade('terminal-bench', 0, evidence))

    def test_swe_requires_executed_tests_and_consistent_reward(self):
        evidence = dict(resolved=False, tests_status={'FAIL_TO_PASS':dict(failure=['test_example'])})
        self.assertEqual(harbor_grade('swebench-verified', 0, evidence), 0)
        self.assertIsNone(harbor_grade('swebench-verified', 1, evidence))
        self.assertIsNone(harbor_grade('swebench-verified', 0, {'resolved':False}))

    def test_appworld_requires_nonempty_official_evaluation(self):
        from harness_benchmark.appworld_adapter import score_evaluation
        self.assertEqual(score_evaluation({'success':False}), 0)
        self.assertEqual(score_evaluation({'passes':['a'], 'failures':[]}), 1)
        with self.assertRaises(ValueError):
            score_evaluation({'passes':[], 'failures':[]})

    def test_swe_bootstrap_keeps_test_body_and_decodes_reference_fields(self):
        from harness_benchmark.swe_verifier import prepare_script
        source = '# official test body\ntest_spec   = make_test_spec(datum)\nuv run parser.py | tee log\n'
        changed = prepare_script(source, 'pytest-dev__pytest-example')
        self.assertTrue(changed.startswith('# official test body\n'))
        spec_code = changed.split('\n',1)[1].split('\nPYTHONPATH=')[0]
        fields = dict(repo='pytest-dev/pytest', version='4.0', instance_id='example',
                      FAIL_TO_PASS='["new_test"]', PASS_TO_PASS='["old_test"]')
        env = dict(datum=fields, json=json)
        exec(spec_code, env)
        self.assertEqual(vars(env['test_spec']), {**fields, 'FAIL_TO_PASS':['new_test'], 'PASS_TO_PASS':['old_test']})
        with self.assertRaises(ValueError):
            prepare_script('new unrecognized grader', 'example')

    def test_terminal_bootstrap_leaves_assertions_and_pytest_arguments_intact(self):
        from harness_benchmark.verifier import bootstrap_script
        tail = 'uvx -p 3.13 -w pytest==8.4.1 pytest /tests/test_outputs.py -rA\necho 0 > /logs/verifier/reward.txt\n'
        source = 'curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh\nsource $HOME/.local/bin/env\n'+tail
        self.assertTrue(bootstrap_script(source).endswith(tail))
        self.assertEqual(bootstrap_script(tail), tail)

    def test_world_task_is_valid_harbor_task(self):
        from harness_benchmark.run import world_task
        from harbor.models.task.task import Task
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            ready = folder/'ready.json'
            save_json(ready, {'prompt':'synthetic task'})
            cfg = dict(appworld_timeout_sec=1800, minimum_cpus=2, minimum_memory_mb=6144)
            task = world_task(folder, ready, {'image_id':'sha256:'+'a'*64}, cfg)
            parsed = Task(task)
            self.assertEqual(parsed.config.environment.memory_mb, 6144)
            self.assertEqual(parsed.config.agent.timeout_sec, 1800)

    def test_grading_copy_satisfies_harbor_containment_without_mutating_source(self):
        from types import SimpleNamespace
        from harbor.models.task.task import Task
        from harbor.models.trial.paths import TrialPaths
        from harness_benchmark.verifier import TerminalVerifier
        from harness_benchmark.swe_verifier import SWEVerifier
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'original'
            (source/'environment').mkdir(parents=True)
            (source/'tests').mkdir()
            (source/'task.toml').write_text('[environment]\ncpus=1\n')
            (source/'instruction.md').write_text('synthetic')
            script = 'test_spec = make_test_spec(datum)\nuv run parser.py | tee log\n'
            (source/'tests/test.sh').write_text(script)
            original = Task(source)
            env = SimpleNamespace(os=original.config.environment.os)
            before = {str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
            for cls in [TerminalVerifier,SWEVerifier]:
                verifier = cls(task=original,trial_paths=TrialPaths(root/cls.__name__),environment=env)
                verifier.trial_paths.verifier_dir.mkdir(parents=True)
                try:
                    dirs, _, _ = verifier._resolve_tests()
                    for path in dirs:
                        verifier.task.paths.validate_input_tree(path)
                    self.assertIsNot(verifier.task,original)
                    self.assertEqual(original.paths.task_dir,source)
                    self.assertEqual(before,{str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()})
                finally:
                    temporary = getattr(verifier,'_bootstrap_tmp',None) or getattr(verifier,'_temporary',None)
                    if temporary: temporary.cleanup()


class MetricsTests(unittest.TestCase):
    def test_overlapping_request_intervals_are_disjoint_and_clipped(self):
        events = [dict(started_at=0, duration_ms=8000, first_content_ms=2000),
                  dict(started_at=4, duration_ms=5000, first_content_ms=1000)]
        wait, generation, outside = phases(events, 1, 10)
        self.assertEqual((wait, generation, outside), (1,7,1))
        self.assertEqual(sum((wait, generation, outside)), 9)

    def test_native_protocol_token_accounting(self):
        self.assertEqual(tokens(dict(protocol='messages', usage=dict(input_tokens=3,cache_read_input_tokens=4,
            cache_creation_input_tokens=2, output_tokens=5))), (9,4,5))
        self.assertEqual(tokens(dict(protocol='responses', usage=dict(input_tokens=9,
            input_tokens_details=dict(cached_tokens=4), output_tokens=5))), (9,4,5))

    def test_empty_partial_coverage_and_arbitrary_subset_plot(self):
        from harness_benchmark.analyze import analyze, read_table, summarize, collect
        rows = [dict(dataset='terminal-bench',task='synthetic-a',harness='pi', official_score=1, agent_sec=2),
                dict(dataset='terminal-bench',task='synthetic-a',harness='dsh', official_score=None),
                dict(dataset='appworld',task='synthetic-b',harness='pi', official_score=0, agent_sec=3)]
        with tempfile.TemporaryDirectory() as directory:
            summary = analyze(rows, Path(directory)/'analysis')
            self.assertEqual(summary['common_valid_tasks']['terminal-bench'], [])
            dsh = next(g for g in summary['groups'] if g['harness']=='dsh')
            self.assertIsNone(dsh['success_rate'])
            self.assertEqual(len(read_table(Path(directory)/'analysis/trials.csv')), 3)
            self.assertTrue((Path(directory)/'analysis/figures/success_rates.svg').exists())
            # A prepared but entirely unstarted run must remain fully missing.
            save_json(Path(directory)/'plan.json', {'cells':[dict(index=0,dataset='appworld',task='x',harness='pi')]})
            self.assertIsNone(collect(directory)[0]['official_score'])
            analyze(collect(directory), Path(directory)/'empty-analysis')
        with self.assertRaises(ValueError):
            summarize(rows+[rows[0]])


if __name__ == '__main__':
    unittest.main()
