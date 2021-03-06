import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import Module, ModuleList, Linear, Dropout, LayerNorm, Identity, Parameter, init

from .stochastic_depth import DropPath


def covariance(X):
    D = X.shape[-1]
    mean = torch.mean(X, dim=-1).unsqueeze(-1)
    X = X - mean
    if D==1:
        return 1 / (D)  * X @ X.transpose(-1, -2)
    return 1 / (D - 1) * X @ X.transpose(-1, -2)


def riemannian_dist(x1, x2, use_covariance=False):
    if use_covariance:
        x1 = covariance(x1)
        x2 = covariance(x2)

    s = (torch.linalg.inv(x1) @ x2)

    dist = torch.norm(torch.log(s + s.min() + 1.0), dim=-1, keepdim=True)
    b, h, t, _ = dist.shape
    dist = dist.repeat(1, 1, 1, t)
    # print(dist.shape)
    return dist


def log_dist1(x1, x2, use_covariance=True, use_log=False):
    # print(x1.shape)
    if use_covariance:
        x1 = covariance(x1)
        x2 = covariance(x2)

    if use_log:

        d = torch.log(x1 + 1.0) - torch.log(x2 + 1.0)
    else:
        d = x1 - x2
    # print(d.min(),d.max())
    dist = torch.norm(d, dim=-1, keepdim=True)
    b, h, t, _ = dist.shape
    dist = dist.repeat(1, 1, 1, t)
    # print(dist.shape)

    return dist


def log_dist(x1, x2, use_covariance=True, use_log=False):
    # print(x1.shape)
    if use_covariance:
        x1 = covariance(x1)
        x2 = covariance(x2)
    print(x1.shape)
    if use_log:

        d = torch.log(x1 + 1.0) - torch.log(x2 + 1.0)
    else:
        d = x1 - x2
    # print(d.min(),d.max())
    dist = torch.norm(d.unsqueeze(-1), dim=-1)

    print(dist.shape)

    return dist


def cov_frobenius_norm(x1, x2):
    x1 = covariance(x1)
    x2 = covariance(x2)
    # distance
    dots = torch.matmul(x1, x2.transpose(-1, -2)).unsqueeze(2)

    attn_spd = torch.linalg.norm(dots, dim=2)
    return attn_spd


class RiemmanianAttention(nn.Module):
    def __init__(self, dim, num_heads=8, attention_dropout=0.1, projection_dropout=0.1, sequence_length=-1,
                 qkv_bias=True):
        super().__init__()

        self.num_heads = num_heads
        head_dim = dim // self.num_heads
        self.scale = nn.Parameter(torch.tensor(head_dim ** -0.5))
        self.sequence_length = sequence_length
        self.qkv = Linear(dim, dim * 3, bias=qkv_bias)
        # if self.sequence_length != -1:
        #     self.norm = nn.LayerNorm(normalized_shape=(sequence_length))

        self.attn_drop = Dropout(attention_dropout)
        self.proj = Linear(dim, dim)
        self.proj_drop = Dropout(projection_dropout)

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]

        dots = log_dist(q, k, use_covariance=True, use_log=False) #* self.scale
        # if self.sequence_length != -1:
        #     dots = self.norm(dots)
        att = dots.softmax(dim=-1)
        out = torch.matmul(self.attn_drop(att), v)

        out = out.permute(0, 2, 1, 3).reshape(B, N, C)
        return self.proj_drop(self.proj(out)),att


class RiemmanianEncoderLayer(Module):
    """
    Inspired by torch.nn.TransformerEncoderLayer and timm.
    """

    def __init__(self, d_model, nhead, dim_feedforward=2048, dropout=0.1,
                 attention_dropout=0.1, drop_path_rate=0.1, sequence_length=-1):
        super(RiemmanianEncoderLayer, self).__init__()
        self.pre_norm = LayerNorm(d_model)
        self.self_attn = RiemmanianAttention(dim=d_model, num_heads=nhead,
                                             attention_dropout=attention_dropout, projection_dropout=dropout,
                                             sequence_length=sequence_length)

        self.linear1 = Linear(d_model, dim_feedforward)
        self.dropout1 = Dropout(dropout)
        self.norm1 = LayerNorm(d_model)
        self.linear2 = Linear(dim_feedforward, d_model)
        self.dropout2 = Dropout(dropout)

        self.drop_path = DropPath(drop_path_rate) if drop_path_rate > 0 else Identity()

        self.activation = F.gelu

    def forward(self, src: torch.Tensor, *args, **kwargs) -> torch.Tensor:
        x,w = self.self_attn(self.pre_norm(src))
        src = src + self.drop_path(x)
        src = self.norm1(src)
        src2 = self.linear2(self.dropout1(self.activation(self.linear1(src))))
        src = src + self.drop_path(self.dropout2(src2))
        return src,w


class RiemmanianformerClassifier(Module):
    def __init__(self,
                 seq_pool=True,
                 embedding_dim=768,
                 num_layers=12,
                 num_heads=12,
                 mlp_ratio=4.0,
                 num_classes=1000,
                 dropout=0.1,
                 attention_dropout=0.1,
                 stochastic_depth=0.1,
                 positional_embedding='learnable',
                 use_grassman=True,
                 sequence_length=None):
        super().__init__()
        positional_embedding = positional_embedding if \
            positional_embedding in ['sine', 'learnable', 'none'] else 'sine'
        dim_feedforward = int(embedding_dim * mlp_ratio)
        self.embedding_dim = embedding_dim
        self.sequence_length = sequence_length
        self.seq_pool = seq_pool

        assert sequence_length is not None or positional_embedding == 'none', \
            f"Positional embedding is set to {positional_embedding} and" \
            f" the sequence length was not specified."

        if not seq_pool:
            sequence_length += 1
            self.class_emb = Parameter(torch.zeros(1, 1, self.embedding_dim),
                                       requires_grad=True)
        else:
            self.attention_pool = Linear(self.embedding_dim, 1)

        if positional_embedding != 'none':
            if positional_embedding == 'learnable':
                self.positional_emb = Parameter(torch.zeros(1, sequence_length, embedding_dim),
                                                requires_grad=True)
                init.trunc_normal_(self.positional_emb, std=0.2)
            else:
                self.positional_emb = Parameter(self.sinusoidal_embedding(sequence_length, embedding_dim),
                                                requires_grad=False)
        else:
            self.positional_emb = None

        self.dropout = Dropout(p=dropout)
        dpr = [x.item() for x in torch.linspace(0, stochastic_depth, num_layers)]
        self.blocks = ModuleList([
            RiemmanianEncoderLayer(d_model=embedding_dim, nhead=num_heads,
                                   dim_feedforward=dim_feedforward, dropout=dropout,
                                   attention_dropout=attention_dropout, drop_path_rate=dpr[i],
                                   sequence_length=sequence_length)
            for i in range(num_layers)])
        self.norm = LayerNorm(embedding_dim)

        self.fc = Linear(embedding_dim, num_classes)
        self.apply(self.init_weight)

    def forward(self, x, return_attention=False):
        if self.positional_emb is None and x.size(1) < self.sequence_length:
            x = F.pad(x, (0, 0, 0, self.n_channels - x.size(1)), mode='constant', value=0)

        if not self.seq_pool:
            cls_token = self.class_emb.expand(x.shape[0], -1, -1)
            x = torch.cat((cls_token, x), dim=1)

        if self.positional_emb is not None:
            x += self.positional_emb

        x = self.dropout(x)

        for blk in self.blocks:
            x,w = blk(x)
        x = self.norm(x)

        if self.seq_pool:
            x = torch.matmul(F.softmax(self.attention_pool(x), dim=1).transpose(-1, -2), x).squeeze(-2)
        else:
            x = x[:, 0]

        x = self.fc(x)
        if return_attention:
            return x,w
        return x

    @staticmethod
    def init_weight(m):
        if isinstance(m, Linear):
            init.trunc_normal_(m.weight, std=.02)
            if isinstance(m, Linear) and m.bias is not None:
                init.constant_(m.bias, 0)
        elif isinstance(m, LayerNorm):
            init.constant_(m.bias, 0)
            init.constant_(m.weight, 1.0)

    @staticmethod
    def sinusoidal_embedding(n_channels, dim):
        pe = torch.FloatTensor([[p / (10000 ** (2 * (i // 2) / dim)) for i in range(dim)]
                                for p in range(n_channels)])
        pe[:, 0::2] = torch.sin(pe[:, 0::2])
        pe[:, 1::2] = torch.cos(pe[:, 1::2])
        return pe.unsqueeze(0)
