"""Filter classifier"""

import json
import logging
import collections
import math
import scipy.optimize

import numpy as np
import pandas as pd
from pandas.io.json import json_normalize
import sklearn.linear_model
from sklearn.metrics import accuracy_score, confusion_matrix, roc_auc_score, log_loss

from opustools.util import file_open

logger = logging.getLogger(__name__)


def load_dataframe(data_file):
    """Load normalized scores dataframe from a jsonlines file"""
    data = []
    with file_open(data_file) as dfile:
        for line in dfile:
            try:
                data.append(json.loads(line))
            except json.decoder.JSONDecodeError as err:
                logger.error(line)
                raise err
    return pd.DataFrame(json_normalize(data))


def standardize_dataframe_scores(df, features, means_stds=None):
    """Normalize, zero average, and set direction for scores in each column"""
    new_df = pd.DataFrame()
    if not means_stds:
        means_stds = {}
        for column in df:
            x = df[column].to_numpy()
            if features[column].get('clean-direction', 'high') == 'low':
                direction = -1
            else:
                direction = 1
            means_stds[column] = (x.mean(), x.std(), direction)
    for column in features:
        x = df[column].to_numpy()
        mean, std, direction = means_stds[column]
        if std == 0:
            x = 0
        else:
            x = direction * (x - mean) / std
        new_df[column] = x
    return new_df, means_stds


class Classifier:

    def __init__(self, classname, params, features, standardize_params):
        self.classname = classname
        cls = getattr(sklearn.linear_model, self.classname)
        self.classifier = cls(**params)
        self.features = features
        self.standardize_params = standardize_params

    def standardize(self, df):
        if not self.standardize_params:
            logger.warning("Feature standardization parameters missing")
            return df[self.features]
        return standardize_dataframe_scores(df, self.features, self.standardize_params)[0]

    def train(self, df, labels, standardize=True):
        """Train logistic regression with training_data"""
        df = self.standardize(df) if standardize else df
        self.classifier.fit(df[self.features], labels)

    def write_preds(self, input_fname, output_fname, true_label=None, standardize=True):
        """Write predicted class labels to output file"""
        df_tbc = load_dataframe(input_fname)
        df = self.standardize(df_tbc) if standardize else df_tbc
        logger.info("Classifier labels: %s", self.classifier.classes_)
        labels = self.classifier.predict(df[self.features])
        if true_label:
            true_labels = df_tbc[true_label]
            logger.info('accuracy: %s', accuracy_score(true_labels, labels))
            logger.info('confusion matrix:\n%s', confusion_matrix(true_labels, labels))
        with file_open(output_fname, 'w') as output:
            for label in labels:
                output.write('{}\n'.format(label))

    def write_probs(self, input_fname, output_fname, true_label=None, standardize=True):
        """Write classification probabilities to output file"""
        df_tbc = load_dataframe(input_fname)
        df = self.standardize(df_tbc) if standardize else df_tbc
        logger.info("Classifier labels: %s", self.classifier.classes_)
        probas = self.classifier.predict_proba(df[self.features])
        if true_label:
            true_labels = df_tbc[true_label]
            logger.info('roc_auc: %s', roc_auc_score(true_labels, probas[:,1]))
        with file_open(output_fname, 'w') as output:
            for proba in probas[:,1]:
                output.write('{0:.10f}\n'.format(proba))

    def weights(self):
        """Yield classifier weights"""
        if self.classname == "LogisticRegression":
            yield '(intercept)', self.classifier.intercept_[0]
            for name, value in zip(self.features, self.classifier.coef_[0]):
                yield name, value
        else:
            logger.warning("Method weights unsupported for %s", self.classname)
            return


class TrainClassifier:
    """Classify clean and noisy sentence pairs"""

    def __init__(self, training_scores=None, dev_scores=None, model_type=None,
            model_parameters=None, features=None, **kwargs):
        logger.info("Loading training data")
        self.df_training_data = load_dataframe(training_scores)

        self.features = {}
        for t_key in self.df_training_data.keys():
            for f_key in features.keys():
                if t_key.startswith(f_key):
                    self.features[t_key] = features[f_key]

        self.df_training_data = self.df_training_data[self.features.keys()]
        self.df_training_data, self.means_stds = standardize_dataframe_scores(
                    self.df_training_data, self.features)

        if dev_scores:
            logger.info("Loading development data")
            self.dev_data = load_dataframe(dev_scores)
            self.dev_labels = self.dev_data.pop('label')
            self.dev_data = self.dev_data[self.features.keys()]
            self.dev_data = standardize_dataframe_scores(
                    self.dev_data, self.features, self.means_stds)[0]

        if model_type == None:
            self.model_type = 'LogisticRegression'
        else:
            self.model_type = model_type
        if model_parameters == None:
            self.model_parameters = {}
        else:
            self.model_parameters = model_parameters

    def train_classifier(self, training_data, labels):
        """Train logistic regression with training_data"""
        classifier = Classifier(self.model_type, self.model_parameters,
                                training_data.columns, self.means_stds)
        classifier.train(training_data, labels, standardize=False)
        return classifier

    def get_roc_auc(self, model, dev_data):
        """Calculate ROC AUC for a given model (requires dev_data)"""
        probs = model.classifier.predict_proba(dev_data)
        # pred = model.classifier.predict(dev_data)
        # logger.info("Classifier labels: %s", model.classifier.classes_)
        # logger.info("Predicted labels: %s", collections.Counter(pred))
        return roc_auc_score(self.dev_labels, probs[:,1])

    def get_sse(self, model, training_data, labels):
        """Calculate the residual sum of squares"""
        y_hat = model.classifier.predict(training_data)
        resid = labels - y_hat
        sse = sum(resid**2)+0.01
        return sse

    def get_ce(self, model, training_data, labels):
        """Calculate cross entropy for a given model"""
        y_pred = model.classifier.predict_proba(training_data)
        return log_loss(labels, y_pred)

    def get_aic(self, model, training_data, labels):
        """Calculate AIC for a given model"""
        loss = self.get_ce(model, training_data, labels)
        k = training_data.shape[1] # number of variables
        AIC = 2*k - 2*math.log(loss)
        return AIC

    def get_bic(self, model, training_data, labels):
        """Calculate BIC for a given model"""
        loss = self.get_ce(model, training_data, labels)
        k = training_data.shape[1] # number of variables
        n = training_data.shape[0] # number of observations
        BIC = n*math.log(loss/n) + k*math.log(n)
        #BIC = math.log(n)*k - 2*math.log(loss)
        return BIC

    def get_labels(self, training_data, cutoffs):
        """Get labels for training data based on cutoffs"""
        labels = []
        training_data_dict = training_data.copy().to_dict()
        for i in range(len(training_data.index)):
            label = 1
            for key in cutoffs.keys():
                if training_data_dict[key][i] < cutoffs[key]:
                    label = 0
            labels.append(label)
        return labels

    def get_cutoffs(self, training_data, quantiles, features):
        """Get cutoff values based on discard percentages"""
        cutoffs = {}
        for key in features:
            cutoffs[key] = training_data[key].quantile(quantiles[key])
        return cutoffs

    def find_best_model(self, criterion_name, algorithm='default', options=None):
        """Find the model with the best AIC / BIC / SSE / CE / ROC_AUC"""
        criteria = {'AIC':
                    {'func': self.get_aic, 'best': 'low', 'dev': False},
                'BIC':
                    {'func': self.get_bic, 'best': 'low', 'dev': False},
                'SSE':
                    {'func': self.get_sse, 'best': 'low', 'dev': False},
                'CE':
                    {'func': self.get_ce, 'best': 'low', 'dev': False},
                'ROC_AUC':
                    {'func': self.get_roc_auc, 'best': 'high', 'dev': True}}

        if criterion_name not in criteria.keys():
            raise ValueError('Invalid criterion. Expected one of: {}'.format(
                list(criteria.keys())))
        criterion = criteria[criterion_name]
        features = list(self.features.keys())
        cutoffs = {key: None for key in features}
        bounds = []
        initial = []
        for key, params in self.features.items():
            if 'quantiles' in params:
                min_ = params['quantiles'].get('min', 0)
                max_ = params['quantiles'].get('max', 1)
            else:
                min_, max_ = 0, 1
                logger.warning(
                    "No quantile bounds defined for %s, setting to [%s, %s]",
                    key, min_, max_)
            bounds.append([min_, max_])
            if 'initial' in params.get('quantiles', {}):
                init = params['quantiles']['initial']
            else:
                init = 0.1
                logger.warning(
                    "No initial quantile defined for %s, setting to %s",
                    key, init)
            initial.append(init)
        initial = np.array(initial)

        def cost(qvector):
            best_quantiles = {key: value for key, value in zip(features, qvector)}
            logger.info('Training logistic regression model with quantiles'
                        ' {}'.format(list(best_quantiles.values())))
            if any(q == 0 for q in best_quantiles.values()):
                # Remove unused features
                df_train_copy = self.df_training_data.copy()
                df_dev_copy = self.dev_data.copy()
                active = set(features)
                for key, value in best_quantiles.items():
                    if value == 0:
                        df_train_copy.pop(key)
                        df_dev_copy.pop(key)
                        active.remove(key)
            else:
                df_train_copy = self.df_training_data
                df_dev_copy = self.dev_data
                active = set(features)

            cutoffs = self.get_cutoffs(
                df_train_copy, best_quantiles, active)
            labels = self.get_labels(df_train_copy, cutoffs)
            counts = collections.Counter(labels)
            logger.info("Label counts in data: %s", counts)
            if len(counts) > 1:
                LR = self.train_classifier(df_train_copy, labels)
                if criterion['dev']:
                    crit_value = criterion['func'](LR, df_dev_copy)
                else:
                    crit_value = criterion['func'](LR, df_train_copy, labels)
            else:
                crit_value = np.inf if criterion['best'] == 'low' else -np.inf

            logger.info('Model {crit}: {value}'.format(
                crit=criterion_name, value=crit_value))
            return crit_value if criterion['best'] == 'low' else -crit_value

        if options is None:
            options = {}
        if algorithm == 'default':
            res = self.default_search(cost, initial, bounds=bounds, **options)
            logger.info(res)
            best_quantiles = {key: value for key, value in zip(features, res)}
        else:
            res = scipy.optimize.minimize(
                cost, initial, method=algorithm, bounds=bounds, options=options)
            logger.info(res)
            best_quantiles = {key: value for key, value in zip(features, res.x)}

        df_train_copy = self.df_training_data.copy()
        df_dev_copy = self.dev_data.copy()
        active = set(features)
        for key, value in best_quantiles.items():
            if value == 0:
                df_train_copy.pop(key)
                df_dev_copy.pop(key)
                active.remove(key)
        cutoffs = self.get_cutoffs(
            df_train_copy, best_quantiles, active)
        labels = self.get_labels(df_train_copy, cutoffs)
        LR = self.train_classifier(df_train_copy, labels)
        if criterion['dev']:
            crit_value = criterion['func'](LR, df_dev_copy)
        else:
            crit_value = criterion['func'](LR, df_train_copy, labels)
        return LR, crit_value, best_quantiles

    @staticmethod
    def default_search(costfunc, initial, bounds=None, step_coef=1.25):
        if bounds is None:
            bounds = [(0, 1) for _ in range(len(initial))]
        x = initial.copy()
        cur_x = x
        cur_cost = costfunc(x)
        while True:
            no_change = 0
            for fidx in range(len(initial)):
                new_x = cur_x.copy()
                if new_x[fidx] / step_coef >= bounds[fidx][0]:
                    new_x[fidx] /= step_coef
                    cost = costfunc(new_x)
                    if cost < cur_cost:
                        cur_cost = cost
                        cur_x = new_x
                        continue
                new_x = cur_x.copy()
                if new_x[fidx] * step_coef <= bounds[fidx][1]:
                    new_x[fidx] *= step_coef
                    cost = costfunc(new_x)
                    if cost < cur_cost:
                        cur_cost = cost
                        cur_x = new_x
                        continue
                no_change += 1
            if no_change == len(initial):
                return cur_x
