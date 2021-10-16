# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/trainer.base.ipynb (unless otherwise specified).

__all__ = ['TTSTrainer', 'Tacotron2Loss', 'MellotronTrainer']

# Cell
import os
from pathlib import Path
from pprint import pprint

import torch
from torch.cuda.amp import autocast, GradScaler
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.tensorboard import SummaryWriter
import time

from ..models.common import MelSTFT
from ..utils.plot import (
    plot_attention,
    plot_gate_outputs,
    plot_spectrogram,
)
from ..text.util import text_to_sequence, random_utterance


class TTSTrainer:
    def __init__(self, hparams, rank=None, world_size=None, device=None):
        print("TTSTrainer start", time.perf_counter())
        self.hparams = hparams
        for k, v in hparams.values().items():
            setattr(self, k, v)

        torch.backends.cudnn_enabled = hparams.cudnn_enabled
        self.global_step = 0
        self.rank = rank
        self.world_size = world_size
        self.log_dir = hparams.log_dir
        self.seed = hparams.seed

        if device:
            self.device = device
        elif torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
        self.writer = SummaryWriter(self.log_dir)
        if not hasattr(self, "debug"):
            self.debug = False
        if self.debug:
            print("Running in debug mode with hparams:")
            pprint(hparams.values())
        else:
            print("Initializing trainer with hparams:")
            pprint(hparams.values())

    def init_distributed(self):
        if not self.distributed_run:
            return
        if self.rank is None or self.world_size is None:
            raise Exception(
                "Rank and world size must be provided when distributed training"
            )
        dist.init_process_group(
            "nccl",
            init_method="tcp://localhost:54321",
            rank=self.rank,
            world_size=self.world_size,
        )
        torch.cuda.set_device(self.rank)

    def save_checkpoint(self, checkpoint_name, **kwargs):
        if self.rank is not None and self.rank != 0:
            return
        checkpoint = {}
        for k, v in kwargs.items():
            if hasattr(v, "state_dict"):
                checkpoint[k] = v.state_dict()
            else:
                checkpoint[k] = v
        if not Path(self.checkpoint_path).exists():
            os.makedirs(Path(self.checkpoint_path))
        torch.save(
            checkpoint, os.path.join(self.checkpoint_path, f"{checkpoint_name}.pt")
        )

    def load_checkpoint(self):
        return torch.load(os.path.join(self.warm_start_name))

    def log(self, tag, step, scalar=None, audio=None, image=None, figure=None):
        if self.rank is not None and self.rank != 0:
            return
        if audio is not None:
            self.writer.add_audio(tag, audio, step, sample_rate=self.sample_rate)
        if scalar is not None:
            self.writer.add_scalar(tag, scalar, step)
        if image is not None:
            self.writer.add_image(tag, image, step, dataformats="HWC")
        if figure is not None:
            self.writer.add_figure(tag, figure, step)

    def sample(self, mel, algorithm="griffin-lim", **kwargs):
        if self.rank is not None and self.rank != 0:
            return
        if algorithm == "griffin-lim":
            mel_stft = MelSTFT()
            audio = mel_stft.griffin_lim(mel)
        else:
            raise NotImplemented
        return audio

    def train():
        raise NotImplemented

# Cell
from random import choice, randint
import time
from typing import List

import torch
from torch import nn
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

from ..data_loader import TextMelDataset, TextMelCollate
from ..models.mellotron import Tacotron2
from ..utils.plot import save_figure_to_numpy
from ..utils.utils import reduce_tensor, get_alignment_metrics


class Tacotron2Loss(nn.Module):
    def __init__(self, pos_weight):
        if pos_weight is not None:
            self.pos_weight = torch.tensor(self.pos_weight)
        else:
            self.pos_weight = pos_weight

        super().__init__()

    def forward(self, model_output: List, target: List):
        mel_target, gate_target = target[0], target[1]
        mel_target.requires_grad = False
        gate_target.requires_grad = False
        gate_target = gate_target.view(-1, 1)
        mel_out, mel_out_postnet, gate_out, _ = model_output
        gate_out = gate_out.view(-1, 1)
        mel_loss = nn.MSELoss()(mel_out, mel_target) + nn.MSELoss()(
            mel_out_postnet, mel_target
        )
        gate_loss = nn.BCEWithLogitsLoss(pos_weight=self.pos_weight)(
            gate_out, gate_target
        )
        return mel_loss, gate_loss


class MellotronTrainer(TTSTrainer):
    REQUIRED_HPARAMS = [
        "audiopaths_and_text",
        "checkpoint_path",
        "epochs",
        "mel_fmax",
        "mel_fmin",
        "n_mel_channels",
        "text_cleaners",
        "pos_weight",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.n_frames_per_step_current = self.n_frames_per_step_initial
        self.reduction_window_idx = 0

    def log_training(
        self,
        model,
        X,
        y_pred,
        y,
        loss,
        mel_loss,
        gate_loss,
        grad_norm,
        step_duration_seconds,
    ):
        print(f"Loss: {loss}")
        self.log("Loss/train", self.global_step, scalar=loss)
        self.log("MelLoss/train", self.global_step, scalar=mel_loss)
        self.log("GateLoss/train", self.global_step, scalar=gate_loss)
        self.log("GradNorm", self.global_step, scalar=grad_norm.item())
        self.log("LearningRate", self.global_step, scalar=self.learning_rate)
        self.log(
            "StepDurationSeconds",
            self.global_step,
            scalar=step_duration_seconds,
        )

        if self.global_step % self.steps_per_sample == 0:
            _, mel_out_postnet, gate_outputs, alignments, *_ = y_pred
            mel_target, gate_target = y
            alignment_metrics = get_alignment_metrics(alignments)
            alignment_diagonalness = alignment_metrics["diagonalness"]
            alignment_max = alignment_metrics["max"]
            sample_idx = randint(0, mel_out_postnet.size(0) - 1)
            audio = self.sample(mel=mel_out_postnet[sample_idx])
            self.log(
                "AlignmentDiagonalness/train",
                self.global_step,
                scalar=alignment_diagonalness,
            )
            self.log("AlignmentMax/train", self.global_step, scalar=alignment_max)
            self.log("AudioSample/train", self.global_step, audio=audio)
            self.log(
                "MelPredicted/train",
                self.global_step,
                image=save_figure_to_numpy(
                    plot_spectrogram(mel_out_postnet[sample_idx].data.cpu())
                ),
            )
            self.log(
                "MelTarget/train",
                self.global_step,
                image=save_figure_to_numpy(
                    plot_spectrogram(mel_target[sample_idx].data.cpu())
                ),
            )
            self.log(
                "Gate/train",
                self.global_step,
                image=save_figure_to_numpy(
                    plot_gate_outputs(
                        gate_targets=gate_target[sample_idx].data.cpu(),
                        gate_outputs=gate_outputs[sample_idx].data.cpu(),
                    )
                ),
            )
            input_length = X[1][sample_idx].item()
            output_length = X[4][sample_idx].item()
            self.log(
                "Attention/train",
                self.global_step,
                image=save_figure_to_numpy(
                    plot_attention(
                        alignments[sample_idx].data.cpu().transpose(0, 1),
                        encoder_length=input_length,
                        decoder_length=output_length,
                    )
                ),
            )

            self.sample_inference(model)

    def log_validation(self, X, y_pred, y, mean_loss, mean_mel_loss, mean_gate_loss):
        print(f"Average loss: {mean_loss}")
        self.log("Loss/val", self.global_step, scalar=mean_loss)
        self.log("MelLoss/val", self.global_step, scalar=mean_mel_loss)
        self.log("GateLoss/val", self.global_step, scalar=mean_gate_loss)
        # Generate the sample from a random item from the last y_pred batch.
        mel_target, gate_target = y
        _, mel_out_postnet, gate_outputs, alignments, *_ = y_pred
        alignment_metrics = get_alignment_metrics(alignments)
        alignment_diagonalness = alignment_metrics["diagonalness"]
        alignment_max = alignment_metrics["max"]

        sample_idx = randint(0, mel_out_postnet.size(0) - 1)
        audio = self.sample(mel=mel_out_postnet[sample_idx])

        self.log(
            "AlignmentDiagonalness/val", self.global_step, scalar=alignment_diagonalness
        )
        self.log("AlignmentMax/val", self.global_step, scalar=alignment_max)
        self.log("AudioSample/val", self.global_step, audio=audio)
        self.log(
            "MelPredicted/val",
            self.global_step,
            image=save_figure_to_numpy(
                plot_spectrogram(mel_out_postnet[sample_idx].data.cpu())
            ),
        )
        self.log(
            "MelTarget/val",
            self.global_step,
            image=save_figure_to_numpy(
                plot_spectrogram(mel_target[sample_idx].data.cpu())
            ),
        )
        self.log(
            "Gate/val",
            self.global_step,
            image=save_figure_to_numpy(
                plot_gate_outputs(
                    gate_targets=gate_target[sample_idx].data.cpu(),
                    gate_outputs=gate_outputs[sample_idx].data.cpu(),
                )
            ),
        )
        input_length = X[1][sample_idx].item()
        output_length = X[4][sample_idx].item()
        self.log(
            "Attention/val",
            self.global_step,
            image=save_figure_to_numpy(
                plot_attention(
                    alignments[sample_idx].data.cpu().transpose(0, 1),
                    encoder_length=input_length,
                    decoder_length=output_length,
                )
            ),
        )

    def adjust_frames_per_step(
        self,
        model: Tacotron2,
        train_loader: DataLoader,
        sampler,
        collate_fn: TextMelCollate,
    ):
        """If necessary, adjust model and loader's n_frames_per_step."""
        if not self.reduction_window_schedule:
            return train_loader, sampler, collate_fn
        settings = self.reduction_window_schedule[self.reduction_window_idx]
        if settings["until_step"] is None:
            return train_loader, sampler, collate_fn
        if self.global_step < settings["until_step"]:
            return train_loader, sampler, collate_fn
        old_fps = settings["n_frames_per_step"]
        while settings["until_step"] and self.global_step >= settings["until_step"]:
            self.reduction_window_idx += 1
            settings = self.reduction_window_schedule[self.reduction_window_idx]
        fps = settings["n_frames_per_step"]
        bs = settings["batch_size"]
        print(f"Adjusting frames per step from {old_fps} to {fps}")
        self.batch_size = bs
        model.set_current_frames_per_step(fps)
        self.n_frames_per_step_current = fps
        _, _, train_loader, sampler, collate_fn = self.initialize_loader()
        return train_loader, sampler, collate_fn

    def validate(self, **kwargs):
        model = kwargs["model"]
        val_set = kwargs["val_set"]
        collate_fn = kwargs["collate_fn"]
        criterion = kwargs["criterion"]
        sampler = DistributedSampler(val_set) if self.distributed_run else None
        total_loss, total_mel_loss, total_gate_loss = 0, 0, 0
        total_steps = 0
        model.eval()
        with torch.no_grad():
            val_loader = DataLoader(
                val_set,
                sampler=sampler,
                shuffle=False,
                batch_size=self.batch_size,
                collate_fn=collate_fn,
            )
            for batch in val_loader:
                total_steps += 1
                if self.distributed_run:
                    X, y = model.module.parse_batch(batch)
                else:
                    X, y = model.parse_batch(batch)
                y_pred = model(X)
                mel_loss, gate_loss = criterion(y_pred, y)
                if self.distributed_run:
                    reduced_mel_loss = reduce_tensor(mel_loss, self.world_size).item()
                    reduced_gate_loss = reduce_tensor(gate_loss, self.world_size).item()
                    reduced_val_loss = reduced_mel_loss + reduced_gate_loss
                else:
                    reduced_mel_loss = mel_loss.item()
                    reduced_gate_loss = gate_loss.item()
                reduced_val_loss = reduced_mel_loss + reduced_gate_loss
                total_mel_loss += reduced_mel_loss
                total_gate_loss += reduced_gate_loss
                total_loss += reduced_val_loss

            mean_mel_loss = total_mel_loss / total_steps
            mean_gate_loss = total_gate_loss / total_steps
            mean_loss = total_loss / total_steps
            self.log_validation(X, y_pred, y, mean_loss, mean_mel_loss, mean_gate_loss)
        model.train()

    def sample_inference(self, model):
        if self.rank is not None and self.rank != 0:
            return
        # Generate an audio sample
        with torch.no_grad():
            utterance = torch.LongTensor(
                text_to_sequence(random_utterance(), self.text_cleaners, self.p_arpabet)
            )[None].cuda()
            speaker_id = (
                choice(self.sample_inference_speaker_ids)
                if self.sample_inference_speaker_ids
                else randint(0, self.n_speakers - 1)
            )
            input_ = [utterance, 0, torch.LongTensor([speaker_id]).cuda()]
            if self.include_f0:
                input_.append(torch.zeros([1, 1, 200], device=self.device))
                # 200 can be changed
            model.eval()
            _, mel, gate, attn = model.inference(input_)
            model.train()
            try:
                audio = self.sample(mel[0])
                self.log("SampleInference", self.global_step, audio=audio)
            except Exception as e:
                print(f"Exception raised while doing sample inference: {e}")
                print("Mel shape: ", mel[0].shape)
            self.log(
                "Attention/sample_inference",
                self.global_step,
                image=save_figure_to_numpy(
                    plot_attention(attn[0].data.cpu().transpose(0, 1))
                ),
            )
            self.log(
                "MelPredicted/sample_inference",
                self.global_step,
                image=save_figure_to_numpy(plot_spectrogram(mel[0].data.cpu())),
            )
            self.log(
                "Gate/sample_inference",
                self.global_step,
                image=save_figure_to_numpy(
                    plot_gate_outputs(gate_outputs=gate[0].data.cpu())
                ),
            )

    @property
    def training_dataset_args(self):
        return [
            self.training_audiopaths_and_text,
            self.text_cleaners,
            self.p_arpabet,
            # audio params
            self.n_mel_channels,
            self.sample_rate,
            self.mel_fmin,
            self.mel_fmax,
            self.filter_length,
            self.hop_length,
            self.win_length,
            self.max_wav_value,
            self.include_f0,
            self.pos_weight,
        ]

    @property
    def val_dataset_args(self):
        val_args = [a for a in self.training_dataset_args]
        val_args[0] = self.val_audiopaths_and_text
        return val_args

    def initialize_loader(self):
        train_set = TextMelDataset(
            *self.training_dataset_args,
            debug=self.debug,
            debug_dataset_size=self.batch_size,
        )
        val_set = TextMelDataset(
            *self.val_dataset_args, debug=self.debug, debug_dataset_size=self.batch_size
        )
        collate_fn = TextMelCollate(
            n_frames_per_step=self.n_frames_per_step_current, include_f0=self.include_f0
        )
        sampler = None
        if self.distributed_run:
            self.init_distributed()
            sampler = DistributedSampler(train_set, rank=self.rank)
        train_loader = DataLoader(
            train_set,
            batch_size=self.batch_size,
            shuffle=(sampler is None),
            sampler=sampler,
            collate_fn=collate_fn,
        )
        return train_set, val_set, train_loader, sampler, collate_fn

    def warm_start(self, model, optimizer, start_epoch=0):

        print("Starting warm_start", time.perf_counter())
        checkpoint = self.load_checkpoint()
        # TODO(zach): Once we are no longer using checkpoints of the old format, remove the conditional and use checkpoint["model"] only.
        model_state_dict = (
            checkpoint["model"] if "model" in checkpoint else checkpoint["state_dict"]
        )
        model.from_pretrained(
            model_dict=model_state_dict,
            device=self.device,
            ignore_layers=self.ignore_layers,
        )
        if "optimizer" in checkpoint:
            optimizer.load_state_dict(checkpoint["optimizer"])
        if "iteration" in checkpoint:
            start_epoch = checkpoint["iteration"]
        if "learning_rate" in checkpoint:
            optimizer.param_groups[0]["lr"] = checkpoint["learning_rate"]
            self.learning_rate = checkpoint["learning_rate"]
        if "global_step" in checkpoint:
            self.global_step = checkpoint["global_step"]
            print(f"Adjusted global step to {self.global_step}")
        print("Ending warm_start", time.perf_counter())
        return model, optimizer, start_epoch

    def train(self):
        print("start train", time.perf_counter())
        train_set, val_set, train_loader, sampler, collate_fn = self.initialize_loader()
        criterion = Tacotron2Loss(
            pos_weight=self.pos_weight
        )  # keep higher than 5 to make clips not stretch on

        torch.manual_seed(self.seed)
        model = Tacotron2(self.hparams)
        if self.device == "cuda":
            model = model.cuda()
        if self.distributed_run:
            model = DDP(model, device_ids=[self.rank])
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=self.learning_rate,
            weight_decay=self.weight_decay,
        )
        start_epoch = 0

        if self.warm_start_name:
            model, optimizer, start_epoch = self.warm_start(model, optimizer)

        if self.fp16_run:
            scaler = GradScaler()

        # main training loop
        for epoch in range(start_epoch, self.epochs):
            train_loader, sampler, collate_fn = self.adjust_frames_per_step(
                model, train_loader, sampler, collate_fn
            )
            if self.distributed_run:
                sampler.set_epoch(epoch)
            for batch in train_loader:
                start_time = time.perf_counter()
                self.global_step += 1
                model.zero_grad()
                if self.distributed_run:
                    X, y = model.module.parse_batch(batch)
                else:
                    X, y = model.parse_batch(batch)
                if self.fp16_run:
                    with autocast():
                        y_pred = model(X)
                        mel_loss, gate_loss = criterion(y_pred, y)
                        loss = mel_loss + gate_loss
                else:
                    y_pred = model(X)
                    mel_loss, gate_loss = criterion(y_pred, y)
                    loss = mel_loss + gate_loss

                if self.distributed_run:
                    reduced_mel_loss = reduce_tensor(mel_loss, self.world_size).item()
                    reduced_gate_loss = reduce_tensor(gate_loss, self.world_size).item()
                    reduced_loss = reduce_mel_loss + reduced_gate_loss
                else:
                    reduced_mel_loss = mel_loss.item()
                    reduced_gate_loss = gate_loss.item()
                reduced_loss = reduced_mel_loss + reduced_gate_loss

                if self.fp16_run:
                    scaler.scale(loss).backward()
                    scaler.unscale_(optimizer)
                    grad_norm = torch.nn.utils.clip_grad_norm(
                        model.parameters(), self.grad_clip_thresh
                    )
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    loss.backward()
                    grad_norm = torch.nn.utils.clip_grad_norm(
                        model.parameters(), self.grad_clip_thresh
                    )
                    optimizer.step()
                step_duration_seconds = time.perf_counter() - start_time
                self.log_training(
                    model,
                    X,
                    y_pred,
                    y,
                    reduced_loss,
                    reduced_mel_loss,
                    reduced_gate_loss,
                    grad_norm,
                    step_duration_seconds,
                )
            if epoch % self.epochs_per_checkpoint == 0:
                self.save_checkpoint(
                    f"mellotron_{epoch}",
                    model=model,
                    optimizer=optimizer,
                    iteration=epoch,
                    learning_rate=self.learning_rate,
                    global_step=self.global_step,
                )

            # There's no need to validate in debug mode since we're not really training.
            if self.debug:
                continue
            self.validate(
                model=model,
                val_set=val_set,
                collate_fn=collate_fn,
                criterion=criterion,
            )