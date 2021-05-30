import torch
from einops import rearrange
from torch import nn
from torchdiffeq import odeint_adjoint

from basehelper import *


class Tinvariant_NLayerNN(NLayerNN):
    def forward(self, t, x):
        return super(Tinvariant_NLayerNN, self).forward(x)


class dfwrapper(nn.Module):
    def __init__(self, df, shape, recf=None):
        super(dfwrapper, self).__init__()
        self.df = df
        self.shape = shape
        self.recf = recf

    def forward(self, t, x):
        bsize = x.shape[0]
        if self.recf:
            x = x[:, :-self.recf.osize].reshape(bsize, *self.shape)
            dx = self.df(t, x)
            dr = self.recf(t, x, dx).reshape(bsize, -1)
            dx = dx.reshape(bsize, -1)
            dx = torch.cat([dx, dr], dim=1)
        else:
            x = x.reshape(bsize, *self.shape)
            dx = self.df(t, x)
            dx = dx.reshape(bsize, -1)
        return dx


class NODEintegrate(nn.Module):

    def __init__(self, df, shape=None, tol=tol, adjoint=True, evaluation_times=None, recf=None):
        """
        Create an OdeRnnBase model
            x' = df(x)
            x(t0) = x0
        :param df: a function that computes derivative. input & output shape [batch, channel, feature]
        :param x0: initial condition.
            - if x0 is set to be nn.parameter then it can be trained.
            - if x0 is set to be nn.Module then it can be computed through some network.
        """
        super().__init__()
        self.df = dfwrapper(df, shape, recf) if shape else df
        self.tol = tol
        self.odeint = torchdiffeq.odeint_adjoint if adjoint else torchdiffeq.odeint
        self.evaluation_times = evaluation_times if evaluation_times is not None else torch.Tensor([0.0, 1.0])
        self.shape = shape
        self.recf = recf
        if recf:
            assert shape is not None

    def forward(self, x0):
        """
        Evaluate odefunc at given evaluation time
        :param x0: shape [batch, channel, feature]. Set to None while training.
        :param evaluation_times: time stamps where method evaluates, shape [time]
        :param x0stats: statistics to compute x0 when self.x0 is a nn.Module, shape required by self.x0
        :return: prediction by ode at evaluation_times, shape [time, batch, channel, feature]
        """
        bsize = x0.shape[0]
        if self.shape:
            assert x0.shape[1:] == torch.Size(self.shape), \
                'Input shape {} does not match with model shape {}'.format(x0.shape[1:], self.shape)
            x0 = x0.reshape(bsize, -1)
            if self.recf:
                reczeros = torch.zeros_like(x0[:, :1])
                reczeros = repeat(reczeros, 'b 1 -> b c', c=self.recf.osize)
                x0 = torch.cat([x0, reczeros], dim=1)
            out = odeint(self.df, x0, self.evaluation_times, rtol=self.tol, atol=self.tol)
            if self.recf:
                rec = out[-1, :, -self.recf.osize:]
                out = out[:, :, :-self.recf.osize]
                out = out.reshape(-1, bsize, *self.shape)
                return out, rec
            else:
                return out
        else:
            out = odeint(self.df, x0, self.evaluation_times, rtol=self.tol, atol=self.tol)
            return out

    @property
    def nfe(self):
        return self.df.nfe

    def to(self, device, *args, **kwargs):
        super().to(device, *args, **kwargs)
        self.evaluation_times.to(device)


class NODElayer(NODEintegrate):
    def forward(self, x0):
        out = super(NODElayer, self).forward(x0)
        if isinstance(out, tuple):
            out, rec = out
            return out[-1], rec
        else:
            return out[-1]


'''
class ODERNN(nn.Module):
    def __init__(self, node, rnn, evaluation_times, nhidden):
        super(ODERNN, self).__init__()
        self.t = torch.as_tensor(evaluation_times).float()
        self.n_t = len(self.t)
        self.node = node
        self.rnn = rnn
        self.nhidden = (nhidden,) if isinstance(nhidden, int) else nhidden

    def forward(self, x):
        assert len(x) == self.n_t
        batchsize = x.shape[1]
        out = torch.zeros([self.n_t, batchsize, *self.nhidden]).to(x.device)
        for i in range(1, self.n_t):
            odesol = odeint(self.node, out[i - 1], self.t[i - 1:i + 1])
            h_ode = odesol[1]
            out[i] = self.rnn(h_ode, x[i])
        return out
'''


class NODE(nn.Module):
    def __init__(self, df=None, **kwargs):
        super(NODE, self).__init__()
        self.__dict__.update(kwargs)
        self.df = df
        self.nfe = 0
        self.elem_t = None

    def forward(self, t, x):
        self.nfe += 1
        if self.elem_t is None:
            return self.df(t, x)
        else:
            return self.elem_t * self.df(self.elem_t, x)

    def update(self, elem_t):
        self.elem_t = elem_t.view(*elem_t.shape, 1)


class SONODE(NODE):
    def forward(self, t, x):
        """
        Compute [y y']' = [y' y''] = [y' df(t, y, y')]
        :param t: time, shape [1]
        :param x: [y y'], shape [batch, 2, vec]
        :return: [y y']', shape [batch, 2, vec]
        """
        self.nfe += 1
        v = x[:, 1:, :]
        out = self.df(t, x)
        return torch.cat((v, out), dim=1)


class HeavyBallNODE(NODE):
    def __init__(self, df, actv_h=None, gamma_guess=-3.0, gamma_act='sigmoid', corr=-100, corrf=True):
        super().__init__(df)
        # Momentum parameter gamma
        self.gamma = Parameter([gamma_guess], frozen=False)
        self.gammaact = nn.Sigmoid() if gamma_act == 'sigmoid' else gamma_act
        self.corr = Parameter([corr], frozen=corrf)
        self.sp = nn.Softplus()
        # Activation for dh, GHBNODE only
        self.actv_h = nn.Identity() if actv_h is None else actv_h

    def forward(self, t, x):
        """
        Compute [theta' m' v'] with heavy ball parametrization in
        $$ theta' = -m / sqrt(v + eps) $$
        $$ m' = h f'(theta) - rm $$
        $$ v' = p (f'(theta))^2 - qv $$
        https://www.jmlr.org/papers/volume21/18-808/18-808.pdf
        because v is constant, we change c -> 1/sqrt(v)
        c has to be positive
        :param t: time, shape [1]
        :param x: [theta m], shape [batch, 2, dim]
        :return: [theta' m'], shape [batch, 2, dim]
        """
        self.nfe += 1
        h, m = torch.split(x, 1, dim=1)
        dh = self.actv_h(- m)
        dm = self.df(t, h) - self.gammaact(self.gamma()) * m
        dm = dm + self.sp(self.corr()) * h
        out = torch.cat((dh, dm), dim=1)
        if self.elem_t is None:
            return out
        else:
            return self.elem_t * out

    def update(self, elem_t):
        self.elem_t = elem_t.view(*elem_t.shape, 1, 1)


HBNODE = HeavyBallNODE


class NormedHeavyBall(HeavyBallNODE):
    def __init__(self, df, normbound=100, normf=False, actv_h=None, gamma_guess=-3.0,
                 gamma_act='sigmoid', corr=0):
        super().__init__(df, actv_h=actv_h, gamma_guess=gamma_guess, gamma_act=gamma_act,
                         corr=corr)
        assert normbound >= 1
        self.normf = normf if normf else TVnorm()
        self.normact = NormAct(normbound)

    def forward(self, t, x):
        self.nfe += 1
        theta, m, norm = torch.split(x, 1, dim=1)
        dnorm = self.normf(theta, m).view(norm.shape)
        dtheta = self.actv_h(self.thetalin(theta) - m)  # * self.normact(norm)
        dm = self.df(t, theta) - torch.sigmoid(self.gamma) * m
        dm += self.gamma_corr * theta
        return torch.cat((dtheta, dm, dnorm), dim=1)


"""
class HBNODERNN(ODERNN):
    def __init__(self, df, rnn, evaluation_times, nhidden, *args, **kwargs):
        super(HBNODERNN, self).__init__(df, rnn, evaluation_times, nhidden)
        self.node = HeavyBallNODE(df, *args, **kwargs)

    def forward(self, x):
        assert len(x) == self.n_t
        batchsize = x.shape[1]
        out = torch.zeros([self.n_t, batchsize, 2, *self.nhidden]).to(x.device)
        for i in range(1, self.n_t):
            odesol = odeint(self.node, out[i - 1], self.t[i - 1:i + 1])
            h_ode, m_ode = odesol[1].split(1, dim=1)
            m_rnn = self.rnn(m_ode, x[i])
            out[i] = torch.cat([h_ode, m_rnn], dim=1)
        return out
"""


class ODE_RNN_with_Grad_Listener(nn.Module):
    def __init__(self, ode, rnn, nhid, ic, rnn_out=False, both=False, tol=1e-7):
        super().__init__()
        self.ode = ode
        self.t = torch.Tensor([0, 1])
        self.nhid = [nhid] if isinstance(nhid, int) else nhid
        self.rnn = rnn
        self.tol = tol
        self.rnn_out = rnn_out
        self.ic = ic
        self.both = both

    def forward(self, t, x, multiforecast=None, retain_grad=False):
        """
        --
        :param t: [time, batch]
        :param x: [time, batch, ...]
        :return: [time, batch, *nhid]
        """
        n_t, n_b = t.shape
        h_ode = [None] * (n_t + 1)
        h_rnn = [None] * (n_t + 1)
        h_ode[-1] = h_rnn[-1] = torch.zeros(n_b, *self.nhid)

        if self.ic:
            h_ode[0] = h_rnn[0] = self.ic(rearrange(x, 't b c -> b (t c)')).view((n_b, *self.nhid))
        else:
            h_ode[0] = h_rnn[0] = torch.zeros(n_b, *self.nhid, device=x.device)
        if self.rnn_out:
            for i in range(n_t):
                self.ode.update(t[i])
                h_ode[i] = odeint(self.ode, h_rnn[i], self.t, atol=self.tol, rtol=self.tol)[-1]
                h_rnn[i + 1] = self.rnn(h_ode[i], x[i])
            out = (h_rnn,)
        else:
            for i in range(n_t):
                self.ode.update(t[i])
                h_rnn[i] = self.rnn(h_ode[i], x[i])
                h_ode[i + 1] = odeint(self.ode, h_rnn[i], self.t, atol=self.tol, rtol=self.tol)[-1]
            out = (h_ode,)

        if self.both:
            out = (h_rnn, h_ode)

        out = [torch.stack(h, dim=0) for h in out]

        if multiforecast is not None:
            self.ode.update(torch.ones_like((t[0])))
            forecast = odeint(self.ode, out[-1][-1], multiforecast * 1.0, atol=self.tol, rtol=self.tol)
            out = (*out, forecast)

        if retain_grad:
            self.h_ode = h_ode
            self.h_rnn = h_rnn
            for i in range(n_t + 1):
                if self.h_ode[i].requires_grad:
                    self.h_ode[i].retain_grad()
                if self.h_rnn[i].requires_grad:
                    self.h_rnn[i].retain_grad()

        return out


class ODE_LSTM(ODE_RNN_with_Grad_Listener):
    def __init__(self, ode, lstm_lin, nhid, tol=1e-7):
        super(ODE_LSTM, self).__init__(ode, lstm_lin, nhid, tol=tol)
        self.sigmoid = nn.Sigmoid()
        self.tanh = nn.Tanh()

    def forward(self, t, x):
        """
        --
        :param t: [time, batch]
        :param x: [time, batch, ...]
        :return: [time, batch, *nhid]
        """
        n_t, n_b = t.shape
        h_ode = torch.zeros(n_t + 1, n_b, *self.nhid, device=x.device)
        c_lstm = torch.zeros(n_b, *self.nhid, device=x.device)
        for i in range(n_t):
            self.ode.update(t[i])
            V_lstm = self.rnn(h_ode[i], x[i])
            z_lstm, i_lstm, f_lstm, o_lstm = torch.split(V_lstm, V_lstm.shape[-1] // 4, dim=-1)
            z_lstm = self.tanh(z_lstm)
            i_lstm = self.sigmoid(i_lstm)
            f_lstm = self.sigmoid(f_lstm + 1)
            o_lstm = self.sigmoid(o_lstm)
            c_lstm = z_lstm * i_lstm + c_lstm * f_lstm
            h_lstm = self.tanh(c_lstm) * o_lstm
            h_odein = torch.cat([h_ode[i, :, :1], h_lstm[:, 1:]], dim=1)
            h_ode[i + 1] = odeint(self.ode, h_odein, self.t, atol=self.tol, rtol=self.tol)[1]
        return h_ode


class ODE_RNN(nn.Module):
    def __init__(self, ode, rnn, nhid, ic, rnn_out=False, both=False, tol=1e-7):
        super().__init__()
        self.ode = ode
        self.t = torch.Tensor([0, 1])
        self.nhid = [nhid] if isinstance(nhid, int) else nhid
        self.rnn = rnn
        self.tol = tol
        self.rnn_out = rnn_out
        self.ic = ic
        self.both = both

    def forward(self, t, x, multiforecast=None):
        """
        --
        :param t: [time, batch]
        :param x: [time, batch, ...]
        :return: [time, batch, *nhid]
        """
        n_t, n_b = t.shape
        h_ode = torch.zeros(n_t + 1, n_b, *self.nhid, device=x.device)
        h_rnn = torch.zeros(n_t + 1, n_b, *self.nhid, device=x.device)
        if self.ic:
            h_ode[0] = h_rnn[0] = self.ic(rearrange(x, 't b c -> b (t c)')).view(h_ode[0].shape)
        if self.rnn_out:
            for i in range(n_t):
                self.ode.update(t[i])
                h_ode[i] = odeint(self.ode, h_rnn[i], self.t, atol=self.tol, rtol=self.tol)[-1]
                h_rnn[i + 1] = self.rnn(h_ode[i], x[i])
            out = (h_rnn,)
        else:
            for i in range(n_t):
                self.ode.update(t[i])
                h_rnn[i] = self.rnn(h_ode[i], x[i])
                h_ode[i + 1] = odeint(self.ode, h_rnn[i], self.t, atol=self.tol, rtol=self.tol)[-1]
            out = (h_ode,)

        if self.both:
            out = (h_rnn, h_ode)

        if multiforecast is not None:
            self.ode.update(torch.ones_like((t[0])))
            forecast = odeint(self.ode, out[-1][-1], multiforecast * 1.0, atol=self.tol, rtol=self.tol)
            out = (*out, forecast)

        return out
