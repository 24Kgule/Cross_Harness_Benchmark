"""CPU grading wheels for four CPU-only tasks; official constraints stay intact."""
CPU_TORCH_TASKS={'mteb-retrieve','pytorch-model-cli','pytorch-model-recovery','torch-pipeline-parallelism'}
CPU_TORCH_INDEX='https://download.pytorch.org/whl/cpu'
HF_MIRROR_TASKS={'count-dataset-tokens','mteb-leaderboard','mteb-retrieve'}
HF_DATASET_ENDPOINT='https://hf-mirror.com'


def grading_indexes(task):
    if task not in CPU_TORCH_TASKS:return {}
    return {'UV_INDEX':CPU_TORCH_INDEX,'UV_INDEX_STRATEGY':'unsafe-best-match'}


def dataset_transport(task):
    if task not in HF_MIRROR_TASKS:return {}
    return {'HF_ENDPOINT':HF_DATASET_ENDPOINT}
