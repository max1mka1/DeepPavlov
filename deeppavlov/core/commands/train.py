import datetime
import json
import time
import sys
from collections import OrderedDict

from typing import List, Callable, Tuple

from deeppavlov.core.common.errors import ConfigError
from deeppavlov.core.common.file import read_json
from deeppavlov.core.common.registry import model as get_model
from deeppavlov.core.common.metrics_registry import get_metrics_by_names
from deeppavlov.core.commands.infer import build_agent_from_config
from deeppavlov.core.common.params import from_params
from deeppavlov.core.data.dataset import Dataset
from deeppavlov.core.models.inferable import Inferable
from deeppavlov.core.models.trainable import Trainable
from deeppavlov.core.common import paths


# TODO pass paths to local model configs to agent config.


def train_agent_models(config_path: str):
    usr_dir = paths.USR_PATH
    a = build_agent_from_config(config_path)

    for skill_config in a.skill_configs:
        model_config = skill_config['model']
        model_name = model_config['name']

        if issubclass(get_model(model_name), Trainable):
            reader_config = skill_config['dataset_reader']
            reader = from_params(get_model(reader_config['name']), {})
            data = reader.read(reader_config.get('data_path', usr_dir))

            dataset_config = skill_config['dataset']
            dataset_name = dataset_config['name']
            dataset = from_params(get_model(dataset_name), dataset_config, data=data)

            model = from_params(get_model(model_name), model_config)
            model.train(dataset)
        else:
            print('Model {} is not an instance of Trainable, skip training.'.format(model_name),
                  file=sys.stderr)


def train_model_from_config(config_path: str, mode='train'):
    usr_dir = paths.USR_PATH
    config = read_json(config_path)

    reader_config = config['dataset_reader']
    # NOTE: Why there are no params for dataset reader? Because doesn't have __init__()
    reader = from_params(get_model(reader_config['name']), {})
    data = reader.read(reader_config.get('data_path', usr_dir))

    dataset_config = config['dataset']
    dataset_name = dataset_config['name']
    dataset = from_params(get_model(dataset_name), dataset_config, data=data)

    vocabs = {}
    if 'vocabs' in config:
        for vocab_param_name, vocab_config in config['vocabs'].items():
            vocab_name = vocab_config['name']
            v = from_params(get_model(vocab_name), vocab_config, mode=mode)
            v.train(dataset.iter_all('train'))
            vocabs[vocab_param_name] = v

    model_config = config['model']
    model_name = model_config['name']
    model = from_params(get_model(model_name), model_config, vocabs=vocabs, mode=mode)

    model.train(dataset)

    # The result is a saved to user_dir trained model.


def _fit(model: Trainable, dataset: Dataset, train_config={}):
    model.fit(dataset.iter_all('train'))
    model.save()
    return model


def train_experimental(config_path: str):
    usr_dir = paths.USR_PATH
    config = read_json(config_path)

    reader_config = config['dataset_reader']
    reader = from_params(get_model(reader_config['name']), {})
    data = reader.read(reader_config.get('data_path', usr_dir))

    dataset_config = config['dataset']
    dataset_name = dataset_config['name']
    dataset: Dataset = from_params(get_model(dataset_name), dataset_config, data=data)

    vocabs = {}
    for vocab_param_name, vocab_config in config.get('vocabs', {}).items():
        vocab_name = vocab_config['name']
        v: Trainable = from_params(get_model(vocab_name), vocab_config, mode='train')
        vocabs[vocab_param_name] = _fit(v, dataset)

    model_config = config['model']
    model_name = model_config['name']
    model = from_params(get_model(model_name), model_config, vocabs=vocabs, mode='train')

    train_config = {
        'metrics': ['accuracy'],

        'validate_best': True,
        'test_best': True
    }

    try:
        train_config.update(config['train'])
    except KeyError:
        print('Train config is missing. Populating with default values', file=sys.stderr)

    metrics_functions = list(zip(train_config['metrics'], get_metrics_by_names(train_config['metrics'])))

    if callable(getattr(model, 'train_on_batch', None)):
        _train_batches(model, dataset, train_config, metrics_functions)
    elif callable(getattr(model, 'fit', None)):
        _fit(model, dataset, train_config)
    else:
        'model is not adapted to the experimental_train yet'
        model.train(dataset)
        return

    if train_config['validate_best'] or train_config['test_best']:
        model = from_params(get_model(model_name), model_config, vocabs=vocabs, mode='infer')
        print('Testing the best saved model', file=sys.stderr)

        if train_config['validate_best']:
            report = {
                'valid': _test_model(model, metrics_functions, dataset, train_config.get('batch_size', -1), 'valid')
            }

            print(json.dumps(report, ensure_ascii=False))

        if train_config['test_best']:
            report = {
                'test': _test_model(model, metrics_functions, dataset, train_config.get('batch_size', -1), 'test')
            }

            print(json.dumps(report, ensure_ascii=False))


def _test_model(model: Inferable, metrics_functions: List[Tuple[str, Callable]],
                dataset: Dataset, batch_size=-1, data_type='valid', start_time=None):
    if start_time is None:
        start_time = time.time()

    val_y_true = []
    val_y_predicted = []
    for x, y_true in dataset.batch_generator(batch_size, data_type, shuffle=False):
        y_predicted = list(model.infer(list(x)))
        val_y_true += y_true
        val_y_predicted += y_predicted

    metrics = [(s, f(val_y_true, val_y_predicted)) for s, f in metrics_functions]

    report = {
        'examples_seen': len(val_y_true),
        'metrics': OrderedDict(metrics),
        'time_spent': str(datetime.timedelta(seconds=round(time.time() - start_time)))
    }
    return report


def _train_batches(model: Trainable, dataset: Dataset, train_config: dict,
                   metrics_functions: List[Tuple[str, Callable]]):

    default_train_config = {
        'epochs': 0,
        'batch_size': 1,

        'metric_optimization': 'maximize',

        'validation_patience': 5,
        'val_every_n_epochs': 0,

        'log_every_n_batches': 0,
        # 'show_examples': False,

        'validate_best': True,
        'test_best': True
    }

    train_config = dict(default_train_config, ** train_config)

    if train_config['metric_optimization'] == 'maximize':
        def improved(score, best):
            return score > best
        best = float('-inf')
    elif train_config['metric_optimization'] == 'minimize':
        def improved(score, best):
            return score < best
        best = float('inf')
    else:
        raise ConfigError('metric_optimization has to be one of {}'.format(['maximize', 'minimize']))

    i = 0
    epochs = 0
    examples = 0
    saved = False
    patience = 0
    log_on = train_config['log_every_n_batches'] > 0
    train_y_true = []
    train_y_predicted = []
    start_time = time.time()
    try:
        while True:
            for batch in dataset.batch_generator(train_config['batch_size']):
                x, y_true = batch
                if log_on:
                    y_predicted = list(model.infer(list(x)))
                    train_y_true += y_true
                    train_y_predicted += y_predicted
                model.train_on_batch(batch)
                i += 1
                examples += len(x)

                if train_config['log_every_n_batches'] > 0 and i % train_config['log_every_n_batches'] == 0:
                    metrics = [(s, f(train_y_true, train_y_predicted)) for s, f in metrics_functions]
                    report = {
                        'epochs_done': epochs,
                        'batches_seen': i,
                        'examples_seen': examples,
                        'metrics': dict(metrics),
                        'time_spent': str(datetime.timedelta(seconds=round(time.time() - start_time)))
                    }
                    report = {'train': report}
                    print(json.dumps(report, ensure_ascii=False))
                    train_y_true = []
                    train_y_predicted = []

                    # if train_config['show_examples']:
                    #     for xi, ypi, yti in zip(x, y_predicted, y_true):
                    #         print({'in': xi, 'out': ypi, 'expected': yti})

            epochs += 1

            if train_config['val_every_n_epochs'] > 0 and epochs % train_config['val_every_n_epochs'] == 0:
                report = _test_model(model, metrics_functions, dataset, train_config['batch_size'], 'valid', start_time)

                metrics = list(report['metrics'].items())

                m_name, score = metrics[0]
                if improved(score, best):
                    patience = 0
                    print('New best {} of {}'.format(m_name, score),
                          file=sys.stderr)
                    best = score
                    print('Saving model', file=sys.stderr)
                    model.save()
                    saved = True
                else:
                    patience += 1
                    print('Did not improve on the {} of {}'.format(m_name, best),
                          file=sys.stderr)

                report['impatience'] = patience
                if train_config['validation_patience'] > 0:
                    report['patience_limit'] = train_config['validation_patience']

                report = {'valid': report}
                print(json.dumps(report, ensure_ascii=False))

                if patience >= train_config['validation_patience'] > 0:
                    print('Ran out of patience', file=sys.stderr)
                    break

            if epochs >= train_config['epochs'] > 0:
                break
    except KeyboardInterrupt:
        print('Stopped training', file=sys.stderr)

    if not saved:
        print('Saving model', file=sys.stderr)
        model.save()

    return model
