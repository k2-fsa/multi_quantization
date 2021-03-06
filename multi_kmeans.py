import math
import torch
import random
from torch import nn
from torch import Tensor
from typing import Tuple


# i=9900, ref_loss=0.424, reconstruction_loss=0.436, entropy_loss=0.004, frame_entropy=0.304
# ... for codebook_size=4, num_codebooks=16, frame_entropy_cutoff=0.30000001192092896, entropy_scale=0.01
# i=9900, ref_loss=0.414, reconstruction_loss=0.421, entropy_loss=0.012, frame_entropy=0.457
# ... for codebook_size=16, num_codebooks=8, frame_entropy_cutoff=0.45000001788139343, entropy_scale=0.01
# i=9900, ref_loss=0.407, reconstruction_loss=0.413, entropy_loss=0.161, frame_entropy=0.698
# ... for codebook_size=256, num_codebooks=4, frame_entropy_cutoff=0.675000011920929, entropy_scale=0.01


class MultiKmeansQuantizer(nn.Module):
    def __init__(self, dim: int,
                 codebook_size: int,
                 num_codebooks: int):
        """
        Trainable quantizer that encodes a vector into a sequence of integers (corresponding
        to multiple separate kmeans codebooks), aiming to get the least possible expected squared
        difference.
        """
        super(MultiKmeansQuantizer, self).__init__()

        self.dim = dim
        self.codebook_size = codebook_size
        self.num_codebooks = num_codebooks

        self.centers = nn.Parameter((dim ** -0.5) * torch.randn(num_codebooks, codebook_size, dim))


        # will be exponentiated to become a scale on a distribution, will be trained
        # to get a target frame entropy during training.
        self.frame_entropy_scale = nn.Parameter(torch.zeros(1))


    def get_product_quantizer(self) -> 'MultiKmeansQuantizer':
        """
        Returns a MultiKmeansQuantizer object with codebook_size = self.codebook_size**2 and
           num_codebooks = self.num_codebooks//2, initialized so that each codebook
           in the result is formed from pairs of codebooks in this object.
        """
        new_codebook_size = self.codebook_size ** 2
        new_num_codebooks = self.num_codebooks // 2

        ans = MultiKmeansQuantizer(self.dim,
                                   new_codebook_size,
                                   new_num_codebooks).to(self.centers.device)

        with torch.no_grad():
            for c_out in range(new_num_codebooks):
                c_in1 = 2 * c_out
                c_in2 = 2 * c_out + 1
                for k_in1 in range(self.codebook_size):
                    for k_in2 in range(self.codebook_size):
                        k_out = k_in1 * self.codebook_size + k_in2
                        ans.centers[c_out,k_out,:] = self.centers[c_in1,k_in1,:] + self.centers[c_in2,k_in2,:]
        return ans


    def compute_ref_loss(self, x: Tensor) -> Tensor:
        """
        Compute the loss function, not for optimization, with deterministic indexes using
        argmax not sampling.

        Args:
                x: the Tensor to quantize, of shape (*, dim)

        Returns:   a scalar torch.Tensor containing the relative sum-squared
                    reconstruction loss.
                    It is the sum-squared of (x - reconstructed_x) / sum-squared of x, which will
                    for already-trained models be between 0 and 1, but could be greater than 1
                    at the start of training.
        """
        logits = self._logits(x)

        # reshape logits to (B, self.num_codebooks, self.codebook_size) where B is the
        # product of all dimensions of x except the last one.
        logits = logits.reshape(-1, self.num_codebooks, self.codebook_size)
        B = logits.shape[0]

        # indices: (B, self.num_codebooks)
        indices = torch.argmax(logits, dim=-1)
        # indexes_expanded: (num_codebooks, B, dim)
        indices_expanded = indices.transpose(0, 1).contiguous().unsqueeze(-1).expand(self.num_codebooks, B, self.dim)
        # to_output_reshaped: (num_codebooks, codebook_size, dim)
        to_output_reshaped = self._to_output().reshape(self.num_codebooks, self.codebook_size, self.dim)
        # chosen_codebooks: (num_codebooks, B, dim).
        chosen_codebooks = torch.gather(to_output_reshaped, dim=1, index=indices_expanded)

        # tot_codebooks: (1, B, dim), this is the sum of the chosen rows of `to_output` corresponding
        # to the chosen codebook entries, this would correspond to the approximated x.
        tot_codebooks = chosen_codebooks.sum(dim=0, keepdim=True)
        # tot_error: (1, B, dim), the error of the approximated vs. real x.
        tot_error = tot_codebooks - x.reshape(1, B, self.dim)
        # tot_error_sumsq: scalar, total squared error.  only needed for diagnostics.
        tot_error_sumsq = (tot_error**2).sum()

        x_tot_sumsq = (x ** 2).sum() + 1.0e-20

        rel_tot_error_sumsq = tot_error_sumsq / x_tot_sumsq

        return rel_tot_error_sumsq

    def forward(self, x: Tensor, num_iters: int = 4) -> Tuple[Tensor, Tensor, Tensor, Tensor]:
        """
        Function to be used during training, that gives various loss terms.

        Args:
              x: a Tensor of shape (*, dim) to be approximated
             num_iters: The number of iterations for optimizing the cluster centers
        Returns (indexes, entropy_loss, frame_entropy), where:
              indexes: a LongTensor of shape (*, num_codebooks) containing elements
                   in {0..codebook_size-1}, that (approximately, modulo self.biases)
                   minimize the sum-squared error reconstruction loss.
             entropy_loss: a scalar Tensor of shape (*) that is the difference between
                   the maximum possible average entropy, of log(codebook_size), and the
                   observed class entropy.  Is to be used to encourage classes to have
                   approximately the same probability of being chosen.
            frame_entropy:  average per-frame entropy of distributions from which we
                  selected the indexes.
            reconstruction_loss:  an expectation over the sum-squared error (taken over
                   the choices of indexes on the last iteration of refining indexes),
                   divided by the sum-squared of x.
        """
        assert x.shape[-1] == self.dim
        x_reshaped = x.reshape(-1, self.dim)
        B = x_reshaped.shape[0]

        indexes = torch.randint(self.codebook_size - 1, (B, self.num_codebooks), device=x.device)

        for i in range(num_iters):
            indexes, entropy_loss, frame_entropy, reconstruction_loss = self.refine_indexes_stochastic(x, indexes)

            if False:
                avg_loss = ((self.decode(indexes) - x) ** 2).sum() / ((x ** 2).sum() + 1e-20)
                print(f"iter={i}, avg_loss={avg_loss.item():.3f}")

        indexes = indexes.reshape(*x.shape[:-1], self.num_codebooks)
        return indexes, entropy_loss, frame_entropy, reconstruction_loss


    def encode(self, x: Tensor, num_iters: int = 4) -> Tensor:
        """
        Encode a tensor as integers.
        Args:
              x: a Tensor of shape (*, dim) to be approximated
        Returns (indexes, entropy_loss), where:
              indexes: a LongTensor of shape (*, num_codebooks) containing elements
                   in {0..codebook_size-1}, that can be given to decode(), that should
                   approximately minimize the sum-squared error reconstruction loss.
        """
        assert x.shape[-1] == self.dim
        x_reshaped = x.reshape(-1, self.dim)
        B = x_reshaped.shape[0]

        indexes = torch.zeros(B, self.num_codebooks, dtype=torch.long, device=x.device)

        for _ in range(num_iters):
            indexes = self.refine_indexes(x, indexes)

        indexes = indexes.reshape(*x.shape[:-1], self.num_codebooks)
        return indexes


    def encode_as_bytes(self, x: Tensor) -> Tensor:
        """
        """
        pass

    def decode(self, code: Tensor) -> Tensor:
        """
        Returns the approximated tensor corresponding to the encoding `code`.
        Args:
            code: a Tensor of integer type, of shape (*, self.num_codebooks),
                  containing elements in {0..self.codebook_size - 1}
        Returns:  a Tensor of float, of shape (*, self.dim).
        """
        code_shape = code.shape
        code = code.reshape(-1, self.num_codebooks)
        B = code.shape[0]

        # indexes_expanded has shape (B, self.num_codebooks, 1, self.dim)
        indexes_expanded = code.unsqueeze(-1).unsqueeze(-1).expand(B, self.num_codebooks, 1, self.dim)

        # centers_expanded has shape (B, self.num_codebooks, self.codebook_size, self.dim)
        centers_expanded = self.centers.unsqueeze(0).expand(B, self.num_codebooks, self.codebook_size, self.dim)

        # centers: (B, self.num_codebooks, self.dim)
        centers = torch.gather(centers_expanded, dim=2, index=indexes_expanded).squeeze(2)

        # x: (B, self.dim)
        x = centers.sum(dim=1)
        return x.reshape(*code_shape[:-1], self.dim)

    def refine_indexes(self,
                       x: Tensor,
                       indexes: Tensor) -> Tensor:
        """
        Refine choices of indexes (this is called iteratively starting from
        all-zeros).
        Args:
           x:  A Tensor of shape (B, self.dim) to be approximated.
           indexes: A Tensor of integer type, of shape (B, self.num_codebooks),
                that contains elements in {0..self.codebook_size-1}
         Returns:  A tensor of indexes of shape (B, self.num_codebooks) that
                  will hopefully reduce the error w.r.t. x, better or at least no worse
                  than `indexes`.  This algorithm is not exact, but if the codebooks are
                  fairly orthogonal it should work fine.   If they are not fairly orthogonal
                  it may not optimize well, but hopefully the codebooks will then learn
                  to be more orthogona..
        """
        B = indexes.shape[0]
        # indexes_expanded has shape (B, self.num_codebooks, 1, self.dim)
        indexes_expanded = indexes.unsqueeze(-1).unsqueeze(-1).expand(B, self.num_codebooks, 1, self.dim)
        # centers_expanded has shape (B, self.num_codebooks, self.codebook_size, self.dim)
        centers_expanded = self.centers.unsqueeze(0).expand(B, self.num_codebooks, self.codebook_size, self.dim)

        # cur_centers: (B, self.num_codebooks, 1, self.dim)
        cur_centers = torch.gather(centers_expanded, dim=2, index=indexes_expanded)
        # x_err is of shape (B, 1, 1, self.dim), it is the current error of the approximation vs. x.
        x_err = cur_centers.sum(dim=1, keepdim=True) - x.unsqueeze(1).unsqueeze(2)

        all_centers = self.centers.unsqueeze(0) # (1, num_codebooks, codebook_size, dim)

        # TODO: get modified_neg_sumsq_errs by a more efficient expression.

        modified_errs = x_err - cur_centers + all_centers
        modified_neg_sumsq_errs = -((modified_errs ** 2).sum(dim=-1)) # (B, num_codebooks, codebook_size)

        indexes = modified_neg_sumsq_errs.argmax(dim=2) # (B, num_codebooks)
        return indexes


    def refine_indexes_stochastic(self,
                                  x: Tensor,
                                  indexes: Tensor) -> Tuple[Tensor, Tensor, Tensor, Tensor]:
        """
        Refine choices of indexes (this is called iteratively starting from
        all-zeros).  This version is stochastic.
        Args:
           x:  A Tensor of shape (B, self.dim) to be approximated.
           indexes: A Tensor of integer type, of shape (B, self.num_codebooks),
                that contains elements in {0..self.codebook_size-1}
           training: If true, will take into account self.biases, which will
                in general make the approximation worse but helps control class
                diversity.
         Returns:  (new_indexes, entropy_loss, frame_entropy, reconstruction_loss), where:
                new_indexes: A tensor of indexes of shape (B, self.num_codebooks) that
                  will hopefully reduce the error w.r.t. x, better or at least no worse
                  than `indexes`.  This algorithm is not exact, but if the codebooks are
                  fairly orthogonal it should work fine.   If they are not fairly orthogonal
                  it may not optimize well, but hopefully the codebooks will then learn
                  to be more orthogona..
                entropy_loss: difference between maximum possible entropy over classes
                  (=log(self.codebook_size)), and the observed average entropy over classes
                  (averaged over codebooks).  Will be zero if codebooks all have balanced
                  frequencies.
                frame_entropy: the average per-frame entropy of the distribution from
                  which new_indexes was sampled.
                reconstruction_loss:  an expectation over the sum-squared error (taken over
                   the choices of indexes on the last iteration of refining indexes),
                   divided by the sum-squared of x.

        """
        B = indexes.shape[0]
        # indexes_expanded has shape (B, self.num_codebooks, 1, self.dim)
        indexes_expanded = indexes.unsqueeze(-1).unsqueeze(-1).expand(B, self.num_codebooks, 1, self.dim)
        assert indexes_expanded.shape == (B, self.num_codebooks, 1, self.dim)
        # centers_expanded has shape (B, self.num_codebooks, self.codebook_size, self.dim)
        centers_expanded = self.centers.unsqueeze(0).expand(B, self.num_codebooks, self.codebook_size, self.dim)
        assert centers_expanded.shape == (B, self.num_codebooks, self.codebook_size, self.dim)

        # cur_centers: (B, self.num_codebooks, 1, self.dim)
        cur_centers = torch.gather(centers_expanded, dim=2, index=indexes_expanded)
        assert cur_centers.shape == (B, self.num_codebooks, 1, self.dim)
        # x_err is of shape (B, 1, 1, self.dim), it is the current error of the approximation vs. x.
        x_err = cur_centers.sum(dim=1, keepdim=True) - x.unsqueeze(1).unsqueeze(2)
        assert x_err.shape == (B, 1, 1, self.dim)

        all_centers = self.centers.unsqueeze(0) # (1, num_codebooks, codebook_size, dim)
        assert all_centers.shape == (1, self.num_codebooks, self.codebook_size, self.dim)

        # TODO: get modified_neg_sumsq_errs by a more efficient expression.

        # modified_errs [b][i][j] is the error of (prediction - x) assuming we replaced
        # the i'th codebook's entry with codebook index j.
        modified_errs = x_err - cur_centers + all_centers             # (B, num_codebooks, codebook_size, dim)
        assert modified_errs.shape == centers_expanded.shape
        modified_sumsq_errs = ((modified_errs ** 2).sum(dim=-1)) # (B, num_codebooks, codebook_size)


        # 10.0 is just to make it equilibriate faster.
        # we only want the derivative for frame_entropy to affect frame_entropy_scale.
        scaled_neg_sumsq_errs_detached = modified_sumsq_errs.detach() * -(10.0 * self.frame_entropy_scale).exp()

        # codebook_logprobs_detached: (B, num_codebooks, codebook_size)
        codebook_logprobs_detached = scaled_neg_sumsq_errs_detached.log_softmax(dim=-1)
        # indexes: (B, num_codebooks)
        indexes = torch.distributions.categorical.Categorical(logits=codebook_logprobs_detached).sample()
        codebook_probs_detached = scaled_neg_sumsq_errs_detached.softmax(dim=-1)
        avg_frame_entropy = -(codebook_logprobs_detached * codebook_probs_detached).sum(dim=-1).mean()


        # this time, detach only the frame_entropy_scale.  we're computing the expected sum-squared
        # loss.
        # (B, num_codebooks, codebook_size)
        scaled_neg_sumsq_errs = modified_sumsq_errs * -(10.0 * self.frame_entropy_scale.detach()).exp()
        codebook_probs = scaled_neg_sumsq_errs.softmax(dim=-1)  # (B, num_codebooks, codebook_size)
        # the second term below can be thought of as compensating  for things that are repeated multiple times.
        expected_sumsq_term1 = (codebook_probs * modified_sumsq_errs).sum()
        #expected_sumsq_term2 = (self.num_codebooks // 2) * (x_err ** 2).sum()

        expected_sumsq = expected_sumsq_term1 / self.num_codebooks


        #assert expected_sumsq > 0
        avg_probs = codebook_probs.mean(dim=0)  # (num_codebooks, codebook_size)
        class_entropy = -(avg_probs * (avg_probs + 1.0e-20).log()).sum(dim=1).mean()

        entropy_loss = math.log(self.codebook_size) - class_entropy
        reconstruction_loss = expected_sumsq / (x ** 2).sum()
        return indexes, entropy_loss, avg_frame_entropy, reconstruction_loss




def _test_quantization():
    torch.manual_seed(1)
    dim = 256
    device = torch.device('cuda')
    model = nn.Sequential(
        nn.Linear(dim, dim),
        nn.ReLU(),
        nn.Linear(dim, dim),
        nn.ReLU(),
        nn.LayerNorm(dim),
        nn.Linear(dim, dim),
    ).to(device)


    # out of codebook_size, num_codebooks = (4, 16), (16, 8), (256, 4), all of which
    # give 4 bytes per 512-dimensional vector, the best reconstruction loss
    # SET SIZES:
    codebook_size = 4
    num_codebooks = 16

    quantizer = MultiKmeansQuantizer(dim=dim, codebook_size=codebook_size,
                                     num_codebooks=num_codebooks).to(device)

    target_frame_entropy = 0.2

    entropy_scale = 1.0e-07
    lr=0.001
    num_iters = 3
    for iter in range(num_iters):

        # training quantizer, not model.
        optim = torch.optim.Adam(
            quantizer.parameters(), lr=lr, betas=(0.9, 0.9), eps=1e-9, weight_decay=0.000001
        )

        # We'll choose in the loop how often to step the scheduler.
        scheduler = torch.optim.lr_scheduler.StepLR(optim, step_size=1000, gamma=0.5)

        for i in range(10000):
            B = 600
            x = torch.randn(B, dim, device=device)
            x = model(x)  + 0.05 * x
            # x is the thing we're trying to quantize: the nnet gives it a non-trivial distribution, which is supposed to
            # emulate a typical output of a neural net.  The "+ 0.1 * x" is a kind of residual term which makes sure
            # the output is not limited to a subspace or anything too-easy like that.


            indexes, entropy_loss, frame_entropy, rel_err = quantizer(x)

            #rel_err = ((x - quantizer.decode(indexes)) ** 2).sum() / ((x ** 2).sum() + 1.0e-20)

            if i % 100 == 0:
                ref_loss = ((x - quantizer.decode(quantizer.encode(x))) ** 2).sum() / ((x ** 2).sum() + 1.0e-20)

                print(f"i={i}, ref_loss={ref_loss.item():.3f}, rel_err={rel_err.item():.3f}, "
                      f"entropy_loss={entropy_loss.item():.3f}, "
                      f"frame_entropy={frame_entropy.item():.3f}")

            # There is no point including a scale on the entropy term, since it
            # only affects the biases, whose derivs are not affected by anything
            # else, and since we are using Adam the optimization is unaffected by the scale
            # of these derivatives.
            tot_loss = rel_err + entropy_scale * entropy_loss + (frame_entropy - target_frame_entropy).abs()


            tot_loss.backward()
            optim.step()
            optim.zero_grad()
            scheduler.step()

        print(f"... for codebook_size={quantizer.codebook_size}, num_codebooks={quantizer.num_codebooks}")

        if iter + 1 < num_iters:
            quantizer = quantizer.get_product_quantizer()
            target_frame_entropy *= 1.5
            lr *= 0.5

if __name__ == "__main__":
    _test_quantization()
