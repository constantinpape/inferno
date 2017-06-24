import dill
from datetime import datetime
import os
import subprocess

import torch
from torch.autograd import Variable
from torch.utils.data import DataLoader
from tensorboard_logger import Logger

from ..utils import train_utils as tu
from ..utils import python_utils as pyu
from ..utils import torch_utils as thu
from ..extensions import metrics
from ..extensions import optimizers
from .callbacks import CallbackEngine


class Trainer(object):
    def __init__(self, model=None):
        # Privates
        # Core
        self._model = None
        self._optimizer = None
        self._criterion = None
        self._metric = None

        # Logging
        self._logger = None
        self._last_logged = {}
        # Dummy logger when not logging
        self._dummy_logger = tu.NoLogger

        # Data logistics
        self._loaders = {}
        self._loader_iters = {}

        # Iteration and epoch book-keeping
        self._iteration_count = 0
        self._epoch_count = 0
        self._batch_count = 0

        # GPU and dtype business
        self._use_cuda = False
        self._dtype = 'float'

        # Validation
        self._save_at_best_validation_score = True
        self._best_validation_score = None
        self._is_iteration_with_best_validation_score = False
        self._validate_every = None
        self._num_validation_iterations = None
        # We should exclude the zero-th epoch from validation
        self._last_validated_at_epoch = 0
        # This is to allow a callback to trigger a validation by setting
        # trainer.validate_now = True
        self._validation_externally_triggered = False

        # Checkpointing
        self._save_every = None
        self._save_to_directory = None
        # Nothing to save at epoch 0
        self._last_saved_at_epoch = 0
        # This is to allow a callback to trigger a save by setting trainer.save_now = True
        self._save_externally_triggered = False

        # Stopping conditions
        self._max_num_iterations = None
        self._max_num_epochs = None

        # Callbacks and states
        self._callback_engine = CallbackEngine().bind_trainer(self)
        self._state = {}

        # Public
        if model is not None:
            self.model = model

    @property
    def callbacks(self):
        return self._callback_engine

    @property
    def model(self):
        assert self._model is not None, "Model is not defined yet."
        return self._model

    @model.setter
    def model(self, value):
        self.bind_model(value)

    def bind_model(self, model):
        assert isinstance(model, torch.nn.Module), "Model must be a torch.nn.Module."
        self._model = model

    @property
    def optimizer(self):
        assert self._optimizer is not None, "Optimizer is not set yet."
        return self._optimizer

    @optimizer.setter
    def optimizer(self, value):
        if isinstance(value, str) or callable(value):
            self.build_optimizer(value)
        elif isinstance(value, dict):
            self.build_optimizer(**value)
        else:
            raise NotImplementedError

    def build_optimizer(self, method, **kwargs):
        if isinstance(method, str):
            optimizer_class = getattr(torch.optim, method, None)
            if optimizer_class is None:
                # Look for optimizer in extensions
                optimizer_class = getattr(optimizers, method, None)
            assert optimizer_class is not None, "Optimizer {} not found.".format(method)
        elif callable(method):
            optimizer_class = method
        else:
            raise NotImplementedError
        self._optimizer = optimizer_class(self.model.parameters(), **kwargs)
        return self

    @property
    def criterion(self):
        assert self._criterion is not None, "Criterion is not set yet."
        return self._criterion

    @criterion.setter
    def criterion(self, value):
        if isinstance(value, str) or callable(value):
            self.build_criterion(value)
        elif isinstance(value, dict):
            self.build_criterion(**value)
        else:
            raise NotImplementedError

    def build_criterion(self, method, **kwargs):
        if isinstance(method, str):
            criterion_class = getattr(torch.nn, method)
            assert criterion_class is not None, "Criterion {} not found.".format(method)
        elif callable(method) and isinstance(method, type):
            criterion_class = method
        else:
            raise NotImplementedError
        self._criterion = criterion_class(**kwargs)
        return self

    @property
    def metric(self):
        assert self._metric is not None, "Metric is not set yet."
        return self._metric

    @metric.setter
    def metric(self, value):
        if callable(value) or isinstance(value, str):
            self.build_metric(value)
        else:
            raise NotImplementedError

    def build_metric(self, method):
        if callable(method):
            self._metric = method
        elif isinstance(method, str):
            assert hasattr(metrics, method)
            self._metric = getattr(metrics, method)()
        return self

    @property
    def metric_is_defined(self):
        return self._metric is not None

    @property
    def train_loader(self):
        assert self._loaders.get('train') is not None
        return self._loaders.get('train')

    @train_loader.setter
    def train_loader(self, value):
        assert isinstance(value, DataLoader)
        self._loaders.update({'train': value})

    @property
    def validate_loader(self):
        assert self._loaders.get('validate') is not None
        return self._loaders.get('validate')

    @validate_loader.setter
    def validate_loader(self, value):
        assert isinstance(value, DataLoader)
        self._loaders.update({'validate': value})

    @property
    def logger(self):
        if self._logger is None:
            return self._dummy_logger
        else:
            return self._logger

    @logger.setter
    def logger(self, value):
        if isinstance(value, Logger):
            self._logger = value
        elif isinstance(value, str):
            self.build_logger(value)
        elif isinstance(value, dict):
            assert 'log_directory' in value
            self.build_logger(value.get('log_directory'))
        else:
            raise NotImplementedError

    @property
    def saving_every(self):
        return self._save_every

    def save_at_best_validation_score(self, yes=True):
        self._save_at_best_validation_score = yes
        return self

    @property
    def save_now(self):
        if self._save_externally_triggered:
            # Reset trigger
            self._save_externally_triggered = False
            # Save if externally triggered
            return True
        elif self._is_iteration_with_best_validation_score:
            return self._save_at_best_validation_score
        else:
            # Check if we're saving by epoch
            if self._save_every is not None and self._save_every.by_epoch:
                # Don't save if we've already saved once this epoch
                if self._epoch_count == self._last_saved_at_epoch:
                    return False
                else:
                    # If we haven't saved this epoch, check if we should
                    return self._save_every.match(epoch_count=self._epoch_count)
            else:
                # We're saving by iterations
                return self._save_every is not None and \
                   self._save_every.match(iteration_count=self._iteration_count)

    @save_now.setter
    def save_now(self, value):
        self._save_externally_triggered = bool(value)

    def save_every(self, frequency, to_directory):
        self._save_every = tu.Frequency.build_from(frequency, priority='iterations')
        assert self._save_every.is_consistent
        assert isinstance(to_directory, str)
        if not os.path.exists(to_directory):
            os.mkdir(to_directory)
        else:
            assert os.path.isdir(to_directory)
        self._save_to_directory = to_directory
        return self

    @property
    def validating_every(self):
        return self._validate_every

    @property
    def validate_now(self):
        if self._validation_externally_triggered:
            # Reset trigger
            self._validation_externally_triggered = False
            return True
        elif self._validate_every is not None and self._validate_every.by_epoch:
            # Don't validate if we've done so already this epoch
            if self._last_validated_at_epoch == self._epoch_count:
                return False
            else:
                # If we haven't validated this epoch, check if we should
                return self._validate_every.match(epoch_count=self._epoch_count)
        else:
            return self._validate_every is not None and \
                   self._validate_every.match(iteration_count=self._iteration_count)

    @validate_now.setter
    def validate_now(self, value):
        self._validation_externally_triggered = bool(value)

    def validate_every(self, frequency, for_num_iterations=None):
        self._validate_every = tu.Frequency.build_from(frequency, priority='iterations')
        assert self._validate_every.is_consistent
        self._num_validation_iterations = for_num_iterations
        return self

    def build_logger(self, log_directory):
        # Make directory if it doesn't exist
        if not os.path.exists(log_directory):
            os.mkdir(log_directory)
        else:
            assert os.path.isdir(log_directory)
        # Setup logger
        self._logger = Logger(logdir=log_directory)
        return self

    def update_state(self, key, value):
        self._state.update({key: value})

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def get_current_learning_rate(self):
        learning_rate = self.optimizer.param_groups[0].get('lr', -1.)
        if torch.is_tensor(learning_rate):
            learning_rate = learning_rate[0]
        return learning_rate

    def cuda(self):
        self.model.cuda()
        self._use_cuda = True
        return self

    def is_cuda(self):
        return self._use_cuda

    def to_device(self, objects):
        if isinstance(objects, (list, tuple)):
            return type(objects)([self.to_device(_object) for _object in objects])
        else:
            return objects.cuda() if self._use_cuda else objects

    def cast(self, objects):
        if isinstance(objects, (list, tuple)):
            return type(objects)([self.cast(_object) for _object in objects])
        else:
            # Cast only the float types, while leaving the ints alone
            if objects.__class__.__name__ in ['HalfTensor', 'FloatTensor', 'DoubleTensor']:
                cast_fn = getattr(objects, self._dtype, None)
            else:
                cast_fn = None

            if cast_fn is not None:
                return cast_fn()
            else:
                return objects

    def set_precision(self, dtype):
        assert dtype in ['double', 'float', 'half']
        self._dtype = dtype
        self._model = getattr(self._model, dtype)()
        return self

    @property
    def dtype(self):
        return self._dtype

    @dtype.setter
    def dtype(self, value):
        self.set_precision(value)

    def bind_loader(self, name, loader):
        assert name in ['train', 'validate']
        assert isinstance(loader, DataLoader)
        self._loaders.update({name: loader})
        return self

    def fetch_next_batch(self, from_loader='train', restart_exhausted_generators=True,
                         update_batch_count=True, update_epoch_count_if_generator_exhausted=True):
        # Check if the iterator is built
        if from_loader not in self._loader_iters:
            self._loader_iters.update({from_loader: self._loaders[from_loader].__iter__()})
        # Try to fetch from iterator
        try:
            next_batch = next(self._loader_iters[from_loader])
            if update_batch_count:
                self._batch_count += 1
            return next_batch
        except StopIteration:
            # This if clause prevents infinite recursion if the loader is empty
            if restart_exhausted_generators:
                self._loader_iters.update({from_loader: self._loaders[from_loader].__iter__()})
                # Update epoch count
                if update_epoch_count_if_generator_exhausted:
                    self.next_epoch()
                return self.fetch_next_batch(from_loader, restart_exhausted_generators=False,
                                             update_batch_count=update_batch_count)
            else:
                raise

    def restart_generators(self, of_loader=None):
        if of_loader is None:
            of_loader = self._loaders.keys()
        else:
            assert of_loader in self._loaders.keys(), \
                "Key {} not in loaders ({})".format(of_loader, list(self._loaders))
            of_loader = pyu.to_iterable(of_loader)

        self._loader_iters.update({from_loader: self._loaders[from_loader].__iter__()
                                   for from_loader in of_loader})
        return self

    def wrap_batch(self, batch, requires_grad=False, volatile=False):
        # First, send to device
        batch = self.to_device(batch)
        # Cast to the right dtype
        batch = self.cast(batch)
        # Second, wrap as variable
        batch = type(batch)([Variable(_batch, requires_grad=requires_grad, volatile=volatile)
                             for _batch in batch])
        return batch

    def next_iteration(self):
        self._iteration_count += 1

    def next_epoch(self):
        self._epoch_count += 1
        self._batch_count = 0

    def stop_fitting(self, max_num_iterations=None, max_num_epochs=None):
        # First priority to iteration count
        if max_num_iterations is not None or max_num_epochs is None:
            max_num_iterations = \
                self._max_num_iterations if max_num_iterations is None else max_num_iterations
            assert max_num_iterations is not None
            return self._iteration_count >= max_num_iterations
        else:
            # max_num_epochs is specified. It could be 'auto', in which case we read from the
            # class attribute
            max_num_epochs = self._max_num_epochs \
                if isinstance(max_num_epochs, str) and max_num_epochs.lower() == 'auto' \
                else max_num_epochs
            return self._epoch_count >= max_num_epochs

    def set_max_num_iterations(self, max_num_iterations):
        self._max_num_iterations = max_num_iterations
        return self

    def set_max_num_epochs(self, max_num_epochs):
        self._max_num_epochs = max_num_epochs
        return self

    def fit(self, max_num_iterations=None, max_num_epochs=None):
        # Takes care of:
        #   - dispatching train
        #   - validation
        #   - learning rate scheduling
        #   - saving

        max_num_iterations = self._max_num_iterations if max_num_iterations is None \
            else max_num_iterations

        max_num_epochs = self._max_num_epochs if max_num_epochs is None else max_num_epochs

        while True:
            if self.stop_fitting(max_num_iterations, max_num_epochs):
                self.print("Exceeded max number of iterations / epochs, breaking.")
                break
            # Train
            self.train_for(break_callback=lambda *args: self.stop_fitting(max_num_iterations,
                                                                          max_num_epochs))
            # Check if it's time to validate
            if self.validate_now:
                self.print("Validating.")
                self.validate_for()
            # Check if it's time to save
            if self.save_now:
                self.print("Saving.")
                self.save()

        return self

    def train_for(self, num_iterations=None, break_callback=None):
        # Switch model to train mode
        self.model.train()
        # iteration_num is a local clock. There's the global self._iteration_count that keeps
        # actual track of the number of iterations - this is updated by the call to
        # self.next_iteration().
        iteration_num = 0
        while True:
            if num_iterations is not None and iteration_num > num_iterations:
                self.print("Finished {} iterations. Breaking...".format(num_iterations))
                break
            # Break if break callback asks us to
            if break_callback is not None and break_callback(iteration_num):
                self.print("Breaking on request from callback.")
                break
            self.print("Training iteration {} (batch {} of epoch {})."
                       .format(iteration_num, self._batch_count, self._epoch_count))
            # Zero out the grads
            self.optimizer.zero_grad()
            # No interrupts while computing - a SIGINT could shoot down the driver if
            # done at the wrong time. Not sure if this has something to do with pinned memory
            with pyu.delayed_keyboard_interrupt():
                # Get batch
                batch = self.fetch_next_batch('train')
                # Send to device and wrap as variable
                batch = self.wrap_batch(batch)
                # Separate inputs from targets
                inputs, target = batch[0:-1], batch[-1]
                # Compute prediction
                prediction = self.model(*inputs)
                # Compute loss
                loss = self.criterion(prediction, target)
                # Backprop
                loss.backward()
            # Compute metric
            if self.metric_is_defined:
                error = self.metric(prediction.data, target.data)
                self.update_state('training_error', thu.unwrap(error))
            else:
                error = None
            # Update state
            self.update_state('training_inputs', thu.unwrap(inputs))
            self.update_state('training_target', thu.unwrap(target))
            self.update_state('training_prediction', thu.unwrap(prediction))
            self.update_state('training_loss', thu.unwrap(loss))
            # Update parameters
            self.optimizer.step()
            # Log
            self.log(training_loss=loss.data[0], error=error,
                     learning_rate=self.get_current_learning_rate())
            # Prepare for next iteration
            self.next_iteration()
            # Break if validating or saving. It's important that the next_iteration() method is
            # called before checking validate_now and save_now - because otherwise, the iteration
            # counter is never updated after the first save and validate, resulting in an infinite
            # save + validate loop.
            if self.validate_now:
                self.print("Breaking to validate.")
                break
            if self.save_now:
                self.print("Breaking to save.")
                break
            iteration_num += 1

        return self

    def validate_for(self, num_iterations=None):
        # Average over errors
        validation_error_meter = tu.AverageMeter()
        validation_loss_meter = tu.AverageMeter()
        iteration_num = 0
        num_iterations = \
            self._num_validation_iterations if num_iterations is None else num_iterations

        # Switch to eval mode (e.g. for batchnorm, etc.)
        self.model.eval()

        # Record the epoch we're validating in
        self._last_validated_at_epoch = self._epoch_count

        # If we don't know num_iterations, we're validating the entire dataset - so we might as
        # well restart the loader now
        if num_iterations is None:
            self.restart_generators('validate')

        while True:
            if num_iterations is not None and iteration_num > num_iterations:
                break

            try:
                batch = self.fetch_next_batch('validate',
                                              restart_exhausted_generators=
                                              num_iterations is not None,
                                              update_batch_count=False,
                                              update_epoch_count_if_generator_exhausted=False)
            except StopIteration:
                self.print("Validation generator exhausted, breaking.")
                break

            self.print("Validating iteration {}.".format(iteration_num))
            try:
                # Delay SIGINTs till after computation
                with pyu.delayed_keyboard_interrupt():
                    # Wrap
                    batch = self.wrap_batch(batch, volatile=True)
                    # Separate
                    inputs, target = batch[0:-1], batch[-1]
                    # Comptue output
                    output = self.model(*inputs)
                    # Compute loss
                    loss = self.criterion(output, target)
                batch_size = target.size(0)
                validation_loss_meter.update(loss.data[0], n=batch_size)
                # Compute validation_error
                if self.metric_is_defined:
                    validation_error = self.metric(output.data, target.data)
                    if torch.is_tensor(validation_error):
                        # Convert to float
                        validation_error = validation_error[0]
                    validation_error_meter.update(validation_error, n=batch_size)
                iteration_num += 1
            except RuntimeError:
                self.print("Out of memory, Skipping.")
                pass
        self.print("Done validating. Logging results...")
        # Log
        self.log(validation_loss=validation_loss_meter.avg,
                 validation_error=(validation_error_meter.avg if self.metric_is_defined else None))
        # Report
        self.record_validation_results(
            validation_loss=validation_loss_meter.avg,
            validation_error=(validation_error_meter.avg if self.metric_is_defined else None))

        return self

    def record_validation_results(self, validation_loss, validation_error):
        # Prefer the error metric (if provided). This should be handled with care -
        # validation error should either always not be None, or otherwise.
        validation_score = validation_loss if validation_error is None else validation_error

        # Check if validation error is less than the best so far
        if self._best_validation_score is None or validation_score < self._best_validation_score:
            # Best score so far. The following flag will trigger a save
            self._is_iteration_with_best_validation_score = True
            self._best_validation_score = validation_score

    def log(self, **names_and_values):
        if 'iteration_count' in names_and_values:
            iteration_count = names_and_values.pop('iteration_count')
        else:
            iteration_count = self._iteration_count
        for name, value in names_and_values.items():
            # Check if value is not none
            if value is None:
                continue
            # Obtain scalars from torch tensors
            if torch.is_tensor(value):
                value = value[0]
            self._last_logged.update({name: value})
            self.logger.log_value(name, value, iteration_count)

    def get_config(self, exclude_loader=True):
        # Returns a config dictionary, like __getstate__. Except optionally without the
        # data loaders (which might be yuuuuuge if it contains the data)
        config_dict = {key: val for key, val in self.__dict__.items() if key.startswith('_')}
        # Callbacks can't be robustly pickled because they might contain function handles
        config_dict.pop('_callback_engine')
        # Loader iterators can't be pickled as well
        if '_loader_iters' in config_dict:
            config_dict.pop('_loader_iters')
        if exclude_loader:
            if '_loaders' in config_dict:
                config_dict.pop('_loaders')
        return config_dict

    def set_config(self, config_dict):
        # TODO some sanity checks on config_dict (e.g. whether the model is actually a model, etc)
        self.__dict__.update(config_dict)

    def save(self, exclude_loader=True, stash_best_checkpoint=True):
        # Log the epoch for save_now
        self._last_saved_at_epoch = self._epoch_count
        # Save the state dictionary
        torch.save(self.get_config(exclude_loader=exclude_loader),
                   os.path.join(self._save_to_directory, 'checkpoint.pytorch'),
                   pickle_module=dill)
        if self._is_iteration_with_best_validation_score and stash_best_checkpoint:
            # Do the stashin'
            subprocess.Popen(['cp',
                              os.path.join(self._save_to_directory, 'checkpoint.pytorch'),
                              os.path.join(self._save_to_directory, 'best_checkpoint.pytorch')])
        # This is required to prevent an infinite save loop?
        self._is_iteration_with_best_validation_score = False
        self.print("Saved to {}.".format(self._save_to_directory))
        return self

    def save_model(self, to_directory=None):
        to_directory = self._save_to_directory if to_directory is None else to_directory
        # Save the state dictionary
        torch.save(self.model,
                   os.path.join(to_directory, 'model.pytorch'),
                   pickle_module=dill)
        return self

    def load(self, from_directory=None, best=False):
        from_directory = self._save_to_directory if from_directory is None else from_directory
        assert from_directory is not None, "Nowhere to load from."
        # Get file name
        file_name = 'best_checkpoint.pytorch' if best else 'checkpoint.pytorch'
        # Load the dictionary
        config_dict = torch.load(os.path.join(from_directory, file_name),
                                 pickle_module=dill)
        # This is required to prevent an infinite save loop?
        self._is_iteration_with_best_validation_score = False
        # Set config
        self.set_config(config_dict)
        return self

    def load_model(self, from_directory=None):
        from_directory = self._save_to_directory if from_directory is None else from_directory
        # Load the model
        model = torch.load(from_directory, pickle_module=dill)
        # Set model
        self.model = model
        return self

    def load_(self, *args, **kwargs):
        # Here for legacy reasons - use load instead.
        return self.load(*args, **kwargs)

    def print(self, message):
        print("[+][{}] {}".format(str(datetime.now()), message))

    @classmethod
    def build(cls, model=None, **trainer_config):
        """Factory function to build the trainer."""
        # Check if trainer is to be loaded from file
        if trainer_config.get('load_from_checkpoint'):
            # Load checkpoint config
            trainer = cls(model).save_every(**trainer_config.get('checkpoint_config'))
            trainer.load_()
        else:
            trainer = cls(model) \
                .build_logger(trainer_config.get('log_directory')) \
                .set_max_num_iterations(trainer_config.get('max_num_iterations')) \
                .build_criterion(**trainer_config.get('criterion_config')) \
                .build_optimizer(**trainer_config.get('optimizer_config')) \
                .build_metric(**trainer_config.get('metric_config')) \
                .save_every(**trainer_config.get('checkpoint_config')) \
                .validate_every(**trainer_config.get('validation_config'))

            if trainer_config.get('use_cuda'):
                trainer.cuda()

        return trainer