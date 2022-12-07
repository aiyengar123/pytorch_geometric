from inspect import signature
from math import sqrt
from typing import Any, Dict, List, Optional, Tuple, Union

import torch
from torch import Tensor

from torch_geometric.data import Batch, Data, HeteroData
from torch_geometric.deprecation import deprecated
from torch_geometric.explain.algorithm.utils import (
    clear_masks,
    set_hetero_masks,
    set_masks,
)
from torch_geometric.nn import MessagePassing
from torch_geometric.typing import EdgeType, Metadata, NodeType
from torch_geometric.utils import get_num_hops, k_hop_subgraph, to_networkx


class CaptumModel(torch.nn.Module):
    def __init__(self, model: torch.nn.Module, mask_type: str = "edge",
                 output_idx: Optional[Union[int, List[int]]] = None):
        super().__init__()
        assert mask_type in ['edge', 'node', 'node_and_edge']

        self.mask_type = mask_type
        self.model = model
        self.output_idx = output_idx

    def forward(self, mask, *args):
        """"""
        # The mask tensor, which comes from Captum's attribution methods,
        # contains the number of samples in dimension 0. Since we are
        # working with only one sample, we squeeze the tensors below.
        # TODO: ramona check that dim 0 is the same for all masks
        batch_size = mask.shape[0]
        if type(self.output_idx) == list:
            msg = "`internal_batch_size` needs to be a multiple of the " \
                  "number of objects to be evaluated."
            assert mask.shape[0] % (len(self.output_idx)) == 0, msg
            batch_size = mask.shape[0] // len(self.output_idx)
        if self.mask_type == "edge":
            assert len(args) >= 2, "Expects at least x and edge_index as args."
        if self.mask_type == "node":
            assert len(args) >= 1, "Expects at least edge_index as args."
        if self.mask_type == "node_and_edge":
            assert len(args[1:]) >= 1, "Expects at least edge_index as args."

        # TODO (ramona): pass arguments with keyword and remove the following
        if self.mask_type == 'node':
            x_s, edge_indices = mask, args[0]
            args = args[1:]
        elif self.mask_type == 'edge':
            edge_masks, x_s, edge_indices = mask, args[0], args[1]
            args = args[2:]
        else:
            edge_masks, x_s, edge_indices = args[0], mask, args[1]
            args = args[2:]

        # Merge disjoint graphs into a single graph:
        data_list = []
        for i in range(mask.shape[0]):
            data_list.append(
                Data(
                    x=x_s[i].squeeze(0) if self.mask_type != 'edge' else x_s,
                    edge_index=edge_indices,
                    edge_mask=edge_masks[i].squeeze(0)
                    if 'edge' in self.mask_type else None,
                ))
        batch = Batch.from_data_list(data_list)

        # Set edge mask:
        if 'edge' in self.mask_type:
            set_masks(self.model, batch.edge_mask, batch.edge_index,
                      apply_sigmoid=False)

        x = self.model(batch.x, batch.edge_index, *args)

        # Clear mask:
        if self.mask_type in ['edge', 'node_and_edge']:
            clear_masks(self.model)

        if self.output_idx is not None:
            x = x[batch.ptr[:-1] + torch.tensor(self.output_idx * batch_size)]

        return x


# TODO(jinu) Is there any point of inheriting from `CaptumModel`
class CaptumHeteroModel(CaptumModel):
    def __init__(self, model: torch.nn.Module, mask_type: str, output_id: int,
                 metadata: Metadata):
        super().__init__(model, mask_type, output_id)
        self.node_types = metadata[0]
        self.edge_types = metadata[1]
        self.num_node_types = len(self.node_types)
        self.num_edge_types = len(self.edge_types)

    def _captum_data_to_hetero_data(self, *args) -> Batch:
        """Converts tuple of tensors to `x_dict`, `edge_index_dict` and
        `edge_mask_dict` and merges them to a batch object."""

        if self.mask_type == 'node':
            node_tensors = args[:self.num_node_types]
            edge_index_dict = args[self.num_node_types]
        elif self.mask_type == 'edge':
            edge_mask_tensors = args[:self.num_edge_types]
            x_dict = args[self.num_edge_types]
            edge_index_dict = args[self.num_edge_types + 1]
        else:
            node_tensors = args[:self.num_node_types]
            edge_mask_tensors = args[self.num_node_types:self.num_node_types +
                                     self.num_edge_types]
            edge_index_dict = args[self.num_node_types + self.num_edge_types]

        # Merge disjoint graphs into a single graph:
        data_list = []
        for i in range(args[0].shape[0]):
            if self.mask_type != 'edge':
                node_features = [mask[i].squeeze(0) for mask in node_tensors]
                x_dict = dict(zip(self.node_types, node_features))
            if 'edge' in self.mask_type:
                edge_masks = [mask[i].squeeze(0) for mask in edge_mask_tensors]
                edge_masks_dict = dict(zip(self.edge_types, edge_masks))
            else:
                edge_masks_dict = None
            # Create HeteroData object:
            data = HeteroData()
            for key, value in edge_index_dict.items():
                data[key].edge_index = value
            for key, value in x_dict.items():
                data[key].x = value
            if edge_masks_dict is not None:
                for key, value in edge_masks_dict.items():
                    data[key].edge_mask = value
            data_list.append(data)
        return Batch.from_data_list(data_list)

    def forward(self, *args):
        # Validate args:
        batch_size = args[0].shape[0]
        if type(self.output_idx) == list:
            msg = "`internal_batch_size` needs to be a multiple of the " \
                  "number of objects to be evaluated."
            assert args[0].shape[0] % (len(self.output_idx)) == 0, msg
            batch_size = args[0].shape[0] // len(self.output_idx)

        if self.mask_type == "node":
            assert len(args) >= self.num_node_types + 1
            len_remaining_args = len(args) - (self.num_node_types + 1)
        elif self.mask_type == "edge":
            assert len(args) >= self.num_edge_types + 2
            len_remaining_args = len(args) - (self.num_edge_types + 2)
        else:
            assert len(args) >= self.num_node_types + self.num_edge_types + 1
            len_remaining_args = len(args) - (self.num_node_types +
                                              self.num_edge_types + 1)

        # Get main args:
        batch = self._captum_data_to_hetero_data(*args)

        if 'edge' in self.mask_type:
            set_hetero_masks(self.model, batch.edge_mask_dict,
                             batch.edge_index_dict)

        if len_remaining_args > 0:
            # If there are args other than `x_dict` and `edge_index_dict`
            x = self.model(batch.x_dict, batch.edge_index_dict,
                           *args[-len_remaining_args:])
        else:
            x = self.model(batch.x_dict, batch.edge_index_dict)

        if 'edge' in self.mask_type:
            clear_masks(self.model)

        if self.output_idx is not None:
            # TODO (ramona): Remove this hacky solution
            indices = torch.tensor(self.output_idx * batch_size, dtype=int)
            x = x[torch.tensor(
                [x.shape[0] / batch_size * (i)
                 for i in range(batch_size)], dtype=int) + indices]
        return x


def _to_edge_mask(edge_index: Tensor) -> Tensor:
    num_edges = edge_index.shape[1]
    return torch.ones(num_edges, requires_grad=True, device=edge_index.device)


def _raise_on_invalid_mask_type(mask_type: str):
    if mask_type not in ['node', 'edge', 'node_and_edge']:
        raise ValueError(f"Invalid mask type (got {mask_type})")


def _expand_input(input: Tensor, n_samples: int) -> Tensor:
    return input.repeat(n_samples, *[1] * input.dim())


def to_captum_input(
    x: Union[Tensor, Dict[EdgeType, Tensor]],
    edge_index: Union[Tensor, Dict[EdgeType, Tensor]],
    mask_type: str,
    n_samples: int = 1,
    additional_forward_args: List[Any] = [],
) -> Tuple[Tuple[Tensor], Tuple[Tensor]]:
    r"""Given :obj:`x`, :obj:`edge_index` and :obj:`mask_type`, converts it
    to a format to use in `Captum.ai <https://captum.ai/>`_ attribution
    methods. Returns :obj:`inputs` and :obj:`additional_forward_args`
    required for `Captum`'s :obj:`attribute` functions.
    See :obj:`torch_geometric.nn.to_captum_model` for example usage.

    Args:

        x (Tensor or Dict[NodeType, Tensor]): The node features. For
            heterogenous graphs this is a dictionary holding node featues
            for each node type.
        edge_index(Tensor or Dict[EdgeType, Tensor]): The edge indicies. For
            heterogenous graphs this is a dictionary holding edge index
            for each edge type.
        mask_type (str): Denotes the type of mask to be created with
            a Captum explainer. Valid inputs are :obj:`"edge"`, :obj:`"node"`,
            and :obj:`"node_and_edge"`:
        n_samples (int): The number of samples to explain. (default: :obj:`1`)
        additional_forward_args: Additional forward arguments of the model
            being explained which will be added to the generated
            :obj:`additional_forward_args`. For :class:`Data` this is
            arguments other than :obj:`x` and :obj:`edge_index`. For
            :class:`HeteroData` this is arguments other than :obj:`x_dict` and
            :obj:`edge_index_dict`.
    """
    _raise_on_invalid_mask_type(mask_type)
    additional_forward_args = additional_forward_args[:]
    additional_forward_args.insert(0, edge_index)

    if isinstance(x, Tensor) and isinstance(edge_index, Tensor):
        inputs = []
        if "node" in mask_type:
            inputs.append(_expand_input(x, n_samples))
        if "edge" in mask_type:
            inputs.append(_expand_input(_to_edge_mask(edge_index), n_samples))
        if mask_type == "edge":
            additional_forward_args.insert(0, x)

    elif isinstance(x, Dict) and isinstance(edge_index, Dict):
        node_types = x.keys()
        edge_types = edge_index.keys()
        inputs = []
        if mask_type == "node":
            for key in node_types:
                inputs.append(_expand_input(x[key], n_samples))
        elif mask_type == "edge":
            for key in edge_types:
                inputs.append(
                    _expand_input(_to_edge_mask(edge_index[key]), n_samples))
            additional_forward_args.insert(0, x)
        else:
            for key in node_types:
                inputs.append(_expand_input(x[key], n_samples))
            for key in edge_types:
                inputs.append(
                    _expand_input(_to_edge_mask(edge_index[key]), n_samples))

    else:
        raise ValueError(
            "'x' and 'edge_index' need to be either"
            f"'Dict' or 'Tensor' got({type(x)}, {type(edge_index)})")
    return tuple(inputs), tuple(additional_forward_args)


def captum_output_to_dicts(
    captum_attrs: Tuple[Tensor],
    mask_type: str,
    metadata: Metadata,
) -> Tuple[Optional[List[Dict[NodeType, Tensor]]], Optional[List[Dict[
        EdgeType, Tensor]]]]:
    """Convert the output of `Captum.ai <https://captum.ai/>`_ attribution
    methods which is a tuple of attributions to dictonaries with with node and
    edge attribution tensors. If more than one object is explained, the
    explanations are stored in a list of dictionaries. This function is used
    while explaining :obj:`HeteroData` objects. See
    :obj:`torch_geometric.nn.to_captum_model` for example usage.

    Args:
        captum_attrs (tuple[tensor]): The output of attribution methods.
        mask_type (str): Denotes the type of mask to be created with
            a Captum explainer. Valid inputs are :obj:`"edge"`, :obj:`"node"`,
            and :obj:`"node_and_edge"`:

            1. :obj:`"edge"`: :obj:`captum_attrs` contains only edge
                attributions. The returned tuple has no node attributions but
                one edge attribution dictionary or several dictionaries if
                more than one object is explained. The edge attribution
                dictionary has key `EdgeType` and value edge mask tensor of
                shape :obj:`[num_edges]`.

            2. :obj:`"node"`: :obj:`captum_attrs` contains only node
                attributions. The returned tuple has one node attribution
                dictonary or several dictionaries if more than one object is
                explained. The node attribution dictionary has key `NodeType`
                and value node mask tensor of shape
                :obj:`[num_nodes, num_features]` and no edge attribution.

            3. :obj:`"node_and_edge"`: :obj:`captum_attrs` contains both node
                and edge attributions. The returned tuple contains one node
                attribution dictionary followed by one edge attribution
                dictionary or several dictionaries if more than one object
                is explained.

        metadata (Metadata): The metadata of the heterogeneous graph.
    """
    _raise_on_invalid_mask_type(mask_type)
    node_types = metadata[0]
    edge_types = metadata[1]
    x_attr_dicts, edge_attr_dicts = [], []
    for i in range(captum_attrs[0].shape[0]):
        captum_attrs_i = [
            captum_attr[i].squeeze(0) for captum_attr in captum_attrs
        ]
        if mask_type == "node":
            assert len(node_types) == len(captum_attrs_i)
            x_attr_dicts.append(dict(zip(node_types, captum_attrs_i)))
        elif mask_type == "edge":
            assert len(edge_types) == len(captum_attrs_i)
            edge_attr_dicts.append(dict(zip(edge_types, captum_attrs_i)))
        elif mask_type == "node_and_edge":
            assert len(edge_types) + len(node_types) == len(captum_attrs_i)
            x_attr_dicts.append(
                dict(zip(node_types, captum_attrs_i[:len(node_types)])))
            edge_attr_dicts.append(
                dict(zip(edge_types, captum_attrs_i[len(node_types):])))
    if len(x_attr_dicts) == 1:
        x_attr_dicts = x_attr_dicts[0]
    if len(edge_attr_dicts) == 1:
        edge_attr_dicts = edge_attr_dicts[0]
    return x_attr_dicts, edge_attr_dicts


@deprecated(details='Use `torch_geometric.nn.to_captum_model` instead')
def to_captum(
    model: torch.nn.Module, mask_type: str = "edge",
    output_idx: Optional[int] = None, metadata: Optional[Metadata] = None
) -> Union[CaptumModel, CaptumHeteroModel]:
    r"""
    Alias for :obj:`to_captum_model`.

    .. warning::

        :obj:`~torch_geometric.nn.to_captum` is deprecated and will
        be removed in a future release.
        Use :obj:`torch_geometric.nn.to_captum_model` instead.

    """
    return to_captum_model(model, mask_type, output_idx, metadata)


def to_captum_model(
    model: torch.nn.Module, mask_type: str = "edge",
    output_idx: Optional[Union[int, List[int]]] = None,
    metadata: Optional[Metadata] = None
) -> Union[CaptumModel, CaptumHeteroModel]:
    r"""Converts a model to a model that can be used for
    `Captum.ai <https://captum.ai/>`_ attribution methods.

    Sample code for homogenous graphs:

    .. code-block:: python

        from captum.attr import IntegratedGradients

        from torch_geometric.data import Data
        from torch_geometric.nn import GCN
        from torch_geometric.nn import to_captum_model, to_captum_input

        data = Data(x=(...), edge_index(...))
        model = GCN(...)
        ...  # Train the model.

        # Explain predictions for node `10`:
        mask_type="edge"
        output_idx = 10
        captum_model = to_captum_model(model, mask_type, output_idx)
        inputs, additional_forward_args = to_captum_input(data.x,
                                            data.edge_index,mask_type)

        ig = IntegratedGradients(captum_model)
        ig_attr = ig.attribute(inputs = inputs,
                               target=int(y[output_idx]),
                               additional_forward_args=additional_forward_args,
                               internal_batch_size=1)


    Sample code for heterogenous graphs:

    .. code-block:: python

        from captum.attr import IntegratedGradients

        from torch_geometric.data import HeteroData
        from torch_geometric.nn import HeteroConv
        from torch_geometric.nn import (captum_output_to_dicts,
                                        to_captum_model, to_captum_input)

        data = HeteroData(...)
        model = HeteroConv(...)
        ...  # Train the model.

        # Explain predictions for node `10`:
        mask_type="edge"
        metadata = data.metadata
        output_idx = 10
        captum_model = to_captum_model(model, mask_type, output_idx, metadata)
        inputs, additional_forward_args = to_captum_input(data.x_dict,
                                            data.edge_index_dict, mask_type)

        ig = IntegratedGradients(captum_model)
        ig_attr = ig.attribute(inputs=inputs,
                               target=int(y[output_idx]),
                               additional_forward_args=additional_forward_args,
                               internal_batch_size=1)
        edge_attr_dict = captum_output_to_dicts(ig_attr, mask_type, metadata)


    .. note::
        For an example of using a Captum attribution method within PyG, see
        `examples/captum_explainability.py
        <https://github.com/pyg-team/pytorch_geometric/blob/master/examples/
        captum_explainability.py>`_.

    Args:
        model (torch.nn.Module): The model to be explained.
        mask_type (str, optional): Denotes the type of mask to be created with
            a Captum explainer. Valid inputs are :obj:`"edge"`, :obj:`"node"`,
            and :obj:`"node_and_edge"`. (default: :obj:`"edge"`)
        output_idx (Union[int, List[int]], optional): Index of the output
            element(s) (node or link index) to be explained. With
            :obj:`output_idx` set, the forward function will return only the
            output of the model for the element at the index specified.
            (default: :obj:`None`)
        metadata (Metadata, optional): The metadata of the heterogeneous graph.
            Only required if explaning over a `HeteroData` object.
            (default: :obj: `None`)
    """
    if metadata is None:
        return CaptumModel(model, mask_type, output_idx)
    else:
        return CaptumHeteroModel(model, mask_type, output_idx, metadata)


class Explainer(torch.nn.Module):
    r"""An abstract class for integrating explainability into Graph Neural
    Networks.
    It also provides general visualization methods for graph attributions.

    Args:
        model (torch.nn.Module): The GNN module to explain.
        epochs (int, optional): The number of epochs to train.
            (default: :obj:`None`)
        lr (float, optional): The learning rate to apply.
            (default: :obj:`None`)
        num_hops (int, optional): The number of hops the :obj:`model` is
            aggregating information from.
            If set to :obj:`None`, will automatically try to detect this
            information based on the number of
            :class:`~torch_geometric.nn.conv.message_passing.MessagePassing`
            layers inside :obj:`model`. (default: :obj:`None`)
        return_type (str, optional): Denotes the type of output from
            :obj:`model`. Valid inputs are :obj:`"log_prob"` (the model
            returns the logarithm of probabilities), :obj:`"prob"` (the
            model returns probabilities), :obj:`"raw"` (the model returns raw
            scores) and :obj:`"regression"` (the model returns scalars).
            (default: :obj:`"log_prob"`)
        log (bool, optional): If set to :obj:`False`, will not log any learning
            progress. (default: :obj:`True`)
    """
    def __init__(self, model: torch.nn.Module, lr: Optional[float] = None,
                 epochs: Optional[int] = None, num_hops: Optional[int] = None,
                 return_type: str = 'log_prob', log: bool = False):
        super().__init__()
        assert return_type in ['log_prob', 'prob', 'raw', 'regression']

        self.model = model
        self.lr = lr
        self.epochs = epochs
        self.num_hops = num_hops or get_num_hops(self.model)
        self.return_type = return_type
        self.log = log

    def _flow(self) -> str:
        for module in self.model.modules():
            if isinstance(module, MessagePassing):
                return module.flow
        return 'source_to_target'

    def subgraph(self, node_idx: int, x: Tensor, edge_index: Tensor, **kwargs):
        r"""Returns the subgraph of the given node.

        Args:
            node_idx (int): The node to explain.
            x (Tensor): The node feature matrix.
            edge_index (LongTensor): The edge indices.
            **kwargs (optional): Additional arguments passed to the GNN module.

        :rtype: (Tensor, Tensor, LongTensor, LongTensor, LongTensor, dict)
        """
        num_nodes, num_edges = x.size(0), edge_index.size(1)
        subset, edge_index, mapping, edge_mask = k_hop_subgraph(
            node_idx, self.num_hops, edge_index, relabel_nodes=True,
            num_nodes=num_nodes, flow=self._flow())

        x = x[subset]
        kwargs_new = {}
        for key, value in kwargs.items():
            if torch.is_tensor(value) and value.size(0) == num_nodes:
                kwargs_new[key] = value[subset]
            elif torch.is_tensor(value) and value.size(0) == num_edges:
                kwargs_new[key] = value[edge_mask]
            else:
                kwargs_new[key] = value  # TODO: this is not in PGExplainer
        return x, edge_index, mapping, edge_mask, subset, kwargs_new

    def _to_log_prob(self, x):
        x = x.log_softmax(dim=-1) if self.return_type == 'raw' else x
        x = x.log() if self.return_type == 'prob' else x
        return x

    @torch.no_grad()
    def get_initial_prediction(self, x: Tensor, edge_index: Tensor,
                               batch: Optional[Tensor] = None, **kwargs):
        if batch is not None:
            out = self.model(x, edge_index, batch=batch, **kwargs)
        else:
            out = self.model(x, edge_index, **kwargs)
        if self.return_type == 'regression':
            prediction = out
        else:
            log_logits = self._to_log_prob(out)
            prediction = log_logits.argmax(dim=-1)
        return prediction

    def get_loss(self, out: Tensor, prediction: Tensor,
                 node_idx: Optional[int] = None, **kwargs):
        if self.return_type == 'regression':
            loss = self._loss(out, prediction, node_idx, **kwargs)
        else:
            log_logits = self._to_log_prob(out)
            loss = self._loss(log_logits, prediction, node_idx, **kwargs)
        return loss

    def visualize_subgraph(self, node_idx: Optional[int], edge_index: Tensor,
                           edge_mask: Tensor, y: Optional[Tensor] = None,
                           threshold: Optional[int] = None,
                           edge_y: Optional[Tensor] = None,
                           node_alpha: Optional[Tensor] = None, seed: int = 10,
                           **kwargs):
        r"""Visualizes the subgraph given an edge mask :attr:`edge_mask`.

        Args:
            node_idx (int): The node id to explain.
                Set to :obj:`None` to explain a graph.
            edge_index (LongTensor): The edge indices.
            edge_mask (Tensor): The edge mask.
            y (Tensor, optional): The ground-truth node-prediction labels used
                as node colorings. All nodes will have the same color
                if :attr:`node_idx` is :obj:`-1`.(default: :obj:`None`).
            threshold (float, optional): Sets a threshold for visualizing
                important edges. If set to :obj:`None`, will visualize all
                edges with transparancy indicating the importance of edges.
                (default: :obj:`None`)
            edge_y (Tensor, optional): The edge labels used as edge colorings.
            node_alpha (Tensor, optional): Tensor of floats (0 - 1) indicating
                transparency of each node.
            seed (int, optional): Random seed of the :obj:`networkx` node
                placement algorithm. (default: :obj:`10`)
            **kwargs (optional): Additional arguments passed to
                :func:`nx.draw`.

        :rtype: :class:`matplotlib.axes.Axes`, :class:`networkx.DiGraph`
        """
        import matplotlib.pyplot as plt
        import networkx as nx

        assert edge_mask.size(0) == edge_index.size(1)

        if node_idx is None or node_idx < 0:
            hard_edge_mask = torch.BoolTensor([True] * edge_index.size(1),
                                              device=edge_mask.device)
            subset = torch.arange(edge_index.max().item() + 1,
                                  device=edge_index.device)
            y = None

        else:
            # Only operate on a k-hop subgraph around `node_idx`.
            subset, edge_index, _, hard_edge_mask = k_hop_subgraph(
                node_idx, self.num_hops, edge_index, relabel_nodes=True,
                num_nodes=None, flow=self._flow())

        edge_mask = edge_mask[hard_edge_mask]

        if threshold is not None:
            edge_mask = (edge_mask >= threshold).to(torch.float)

        if y is None:
            y = torch.zeros(edge_index.max().item() + 1,
                            device=edge_index.device)
        else:
            y = y[subset].to(torch.float) / y.max().item()

        if edge_y is None:
            edge_color = ['black'] * edge_index.size(1)
        else:
            colors = list(plt.rcParams['axes.prop_cycle'])
            edge_color = [
                colors[i % len(colors)]['color']
                for i in edge_y[hard_edge_mask]
            ]

        data = Data(edge_index=edge_index, att=edge_mask,
                    edge_color=edge_color, y=y, num_nodes=y.size(0)).to('cpu')
        G = to_networkx(data, node_attrs=['y'],
                        edge_attrs=['att', 'edge_color'])
        mapping = {k: i for k, i in enumerate(subset.tolist())}
        G = nx.relabel_nodes(G, mapping)

        node_args = set(signature(nx.draw_networkx_nodes).parameters.keys())
        node_kwargs = {k: v for k, v in kwargs.items() if k in node_args}
        node_kwargs['node_size'] = kwargs.get('node_size') or 800
        node_kwargs['cmap'] = kwargs.get('cmap') or 'cool'

        label_args = set(signature(nx.draw_networkx_labels).parameters.keys())
        label_kwargs = {k: v for k, v in kwargs.items() if k in label_args}
        label_kwargs['font_size'] = kwargs.get('font_size') or 10

        pos = nx.spring_layout(G, seed=seed)
        ax = plt.gca()
        for source, target, data in G.edges(data=True):
            ax.annotate(
                '', xy=pos[target], xycoords='data', xytext=pos[source],
                textcoords='data', arrowprops=dict(
                    arrowstyle="->",
                    alpha=max(data['att'], 0.1),
                    color=data['edge_color'],
                    shrinkA=sqrt(node_kwargs['node_size']) / 2.0,
                    shrinkB=sqrt(node_kwargs['node_size']) / 2.0,
                    connectionstyle="arc3,rad=0.1",
                ))

        if node_alpha is None:
            nx.draw_networkx_nodes(G, pos, node_color=y.tolist(),
                                   **node_kwargs)
        else:
            node_alpha_subset = node_alpha[subset]
            assert ((node_alpha_subset >= 0) & (node_alpha_subset <= 1)).all()
            nx.draw_networkx_nodes(G, pos, alpha=node_alpha_subset.tolist(),
                                   node_color=y.tolist(), **node_kwargs)

        nx.draw_networkx_labels(G, pos, **label_kwargs)

        return ax, G
