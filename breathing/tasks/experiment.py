import argparse
import itertools
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
from librosa.core import load
from ml.src.dataloader import set_dataloader, set_ml_dataloader
from ml.tasks.base_experiment import base_expt_args, BaseExperimentor, CrossValidator, SeedAverager

DATALOADERS = {'normal': set_dataloader, 'ml': set_ml_dataloader}


def mask_expt_args(parser):
    parser = base_expt_args(parser)
    expt_parser = parser.add_argument_group("Mask Experiment arguments")

    return parser


def label_func(row):
    return row[2]


def set_load_func(data_dir, sr):
    def load_func(path):
        wave = load(f'{data_dir}/{path[0]}', sr=sr)[0]
        return wave.reshape((1, -1))

    return load_func


def train_with_all(val_results, hyperparameter_list, expt_conf, experimentor):
    best_trial_idx = val_results['uar'].argmax()
    best_pattern = patterns[best_trial_idx]

    for idx, param in enumerate(hyperparameter_list):
        expt_conf[param] = best_pattern[idx]

    manifest_df = pd.read_csv(expt_conf['manifest_path'])
    infer_df = manifest_df[manifest_df['file_name'].str.startswith('test')]
    train_devel_df = manifest_df[~manifest_df.index.isin(infer_df.index)]
    infer_df.to_csv(Path(expt_conf['manifest_path']).parent / f'infer_manifest.csv', index=False, header=None)
    train_devel_df.to_csv(Path(expt_conf['manifest_path']).parent / f'train_manifest.csv', index=False, header=None)
    experimentor.cfg[f'infer_path'] = str(Path(expt_conf['manifest_path']).parent / f'infer_manifest.csv')
    experimentor.cfg[f'train_path'] = str(Path(expt_conf['manifest_path']).parent / f'train_manifest.csv')

    pred = experimentor.experiment_without_validation()

    sub_df = pd.read_csv(expt_conf['manifest_path'])
    sub_df = sub_df[sub_df['file_name'].str.startswith('test')]
    sub_df['upper_belt'] = pred
    sub_df.columns = ['filename', 'timeFrame', 'prediction']
    (Path(__file__).resolve().parents[1] / 'output' / 'sub').mkdir(exist_ok=True)
    sub_name = f"{expt_conf['expt_id']}_{'_'.join(list(map(str, best_pattern)))}.csv"
    sub_df.to_csv(Path(__file__).resolve().parents[1] / 'output' / 'sub' / sub_name, index=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='train arguments')
    expt_conf = vars(mask_expt_args(parser).parse_args())
    assert expt_conf['train_path'] != '' or expt_conf['val_path'] != '', \
        'You need to select training, validation data file to training, validation in --train-path, --val-path argments'

    hyperparameters = {
        'window_size': [0.04, 0.08, 0.12],
        'window_stride': [0.02, 0.04, 0.06]
    }

    expt_conf['class_names'] = [0]
    expt_conf['tasy_type'] = 'regress'
    expt_conf['sample_rate'] = 16000

    load_func = set_load_func(Path(expt_conf['manifest_path']).resolve().parents[1] / 'wav', expt_conf['sample_rate'])

    val_metrics = ['loss', 'uar', 'f1']

    manifest_df = pd.read_csv(expt_conf['manifest_path'])
    for phase, part in zip(['train', 'val', 'infer'], ['train', 'devel', 'test']):
        phase_df = manifest_df[manifest_df['file_name'].str.startswith(part)]
        phase_df.to_csv(Path(expt_conf['manifest_path']).parent / f'{phase}_manifest.csv', index=False, header=None)
        expt_conf[f'{phase}_path'] = str(Path(expt_conf['manifest_path']).parent / f'{phase}_manifest.csv')

    patterns = list(itertools.product(*hyperparameters.values()))
    patterns = [(w_size, w_stride) for w_size, w_stride in patterns if w_size > w_stride]
    val_results = pd.DataFrame(np.zeros((len(patterns), len(hyperparameters) + len(val_metrics))),
                               columns=list(hyperparameters.keys()) + val_metrics)

    for i, pattern in enumerate(patterns):
        val_results.iloc[i, :len(hyperparameters)] = pattern
        print(f'Pattern: \n{val_results.iloc[i, :len(hyperparameters)]}')

        for idx, param in enumerate(hyperparameters.keys()):
            expt_conf[param] = pattern[idx]

        groups = None
        if expt_conf['cv_name'] == 'group':
            train_val_manifest = manifest_df[manifest_df['file_name'].str.startswith('test')]
            subjects = pd.DataFrame(train_val_manifest['filename_text'].str.slice(0, -4).unique())
            subjects['group'] = [j % expt_conf['n_splits'] for j in range(len(subjects))]
            subjects = subjects.set_index(0)
            groups = train_val_manifest['filename_text'].str.slice(0, -4).apply(lambda x: subjects.loc[x, 'group'])

        if expt_conf['n_seed_average']:
            experimentor = SeedAverager(expt_conf, load_func, label_func, expt_conf['cv_name'], expt_conf['n_splits'],
                                        groups)
        elif expt_conf['cv_name']:
            experimentor = CrossValidator(expt_conf, load_func, label_func, expt_conf['cv_name'], expt_conf['n_splits'],
                                          groups)
        else:
            experimentor = BaseExperimentor(expt_conf, load_func, label_func)

        with mlflow.start_run():
            result_series, pred = experimentor.experiment_with_validation(val_metrics)

            val_results.loc[i, len(hyperparameters):] = result_series

            # mlflow.log_params(expt_conf)
            mlflow.log_params({hyperparameter: value for hyperparameter, value in zip(hyperparameters.keys(), pattern)})
            mlflow.log_metrics({metric_name: value for metric_name, value in zip(val_metrics, result_series)})

    (Path(__file__).resolve().parents[1] / 'output' / 'metrics').mkdir(exist_ok=True)
    expt_path = Path(__file__).resolve().parents[1] / 'output' / 'metrics' / f"{expt_conf['expt_id']}.csv"
    print(val_results)
    print(val_results.iloc[:, len(hyperparameters):].describe())
    val_results.to_csv(expt_path, index=False)

    # Train with train + devel dataset
    if expt_conf['train_with_all']:
        train_with_all(val_results, list(hyperparameters.keys()), expt_conf, experimentor)
