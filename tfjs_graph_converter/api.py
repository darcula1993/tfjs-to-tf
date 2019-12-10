# Copyright 2019 Patrick Levin. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import base64
import json
import os

import tensorflow as tf
import tensorflowjs as tfjs
import tensorflowjs.converters.common as tfjs_common 
import tfjs_graph_converter.common as common
import tfjs_graph_converter.version as version

from functools import reduce
from tensorflowjs.read_weights import read_weights
from google.protobuf.json_format import ParseDict, MessageToDict

def _find_if_has_key(obj, key, of_type = None):
    """
    Recursively find all objects with a given key in a dictionary

    Args:
        obj: Dictionary to search
        key: Key to find
        of_type: [optional] Type of the referenced item
    
    Returns:
        List of all objects that contain an item with the given key and matching type
    """
    children = lambda item: [val for val in item.values() if isinstance(val, dict)]
    found = []
    stack = children(obj) 
    while len(stack) > 0:
        item = stack.pop()
        if key in item and (of_type is None or isinstance(item[key], of_type)):
            found.append(item)
        stack.extend(children(item))

    return found

def _convert_string_attrs(node):
    """
    Deep search string attributes (labelled "s" in GraphDef proto)
    and convert ascii code lists to base64-encoded strings if necessary
    """
    attr_key = common.TFJS_NODE_ATTR_KEY
    str_key = common.TFJS_ATTR_STRING_VALUE_KEY
    attrs = _find_if_has_key(node[attr_key], key=str_key, of_type=list)
    for attr in attrs:
        array = attr[str_key]
        string = ''.join(map(chr, array))
        binary = string.encode('utf8') 
        attr[str_key] = base64.encodebytes(binary) if len(array) > 0 else None

    return

def _convert_attr_values(message_dict):
    """
    Node attributes in deserialised JSON contain strings as lists of ascii codes.
    The TF GraphDef proto expects these values to be base64 encoded so convert all
    strings here.
    """
    if common.TFJS_NODE_KEY in message_dict:
        nodes = message_dict[common.TFJS_NODE_KEY]
        for node in nodes:
            _convert_string_attrs(node)

    return message_dict

def _convert_graph_def(message_dict):
    """
    Convert JSON to TF GraphDef message

    Args:
        message_dict: deserialised JSON message
    
    Returns:
        TF GraphDef message
    """
    message_dict = _convert_attr_values(message_dict)
    return ParseDict(message_dict, tf.compat.v1.GraphDef())

def _convert_weight_list_to_dict(weight_list):
    """
    Convert list of weight entries to dictionary

    Args:
        weight_list: List of numpy arrays or tensors formatted as
                     {'name': 'entry0', 'data': np.array([1,2,3], 'float32')}

    Returns:
        Dictionary that maps weight names to tensor data, e.g.
        {'entry0:': np.array(...), 'entry1': np.array(...), ...}
    """
    weight_dict = {}
    for entry in weight_list:
        weight_dict[entry[common.TFJS_NAME_KEY]] = entry[common.TFJS_DATA_KEY]
    return weight_dict

def _create_graph(graph_def, weight_dict):
    """
    Create a TF Graph from nodes

    Args:
        graph_def: TF GraphDef message containing the node graph
        weight_dict: Dictionary from node names to tensor data

    Returns:
        TF Graph for inference or saving
    """
    graph = tf.Graph()
    with tf.compat.v1.Session(graph=graph):
        for k, v in weight_dict.items():
            weight_dict[k] = tf.convert_to_tensor(v)
        tf.graph_util.import_graph_def(graph_def, weight_dict, name='')

    return graph

def _convert_graph_model_to_graph(model_json, base_path):
    """
    Convert TFJS JSON model to TF Graph

    Args:
        model_json: JSON dict from TFJS model file
        base_path:  Path to the model file (where to find the model weights)

    Returns:
        TF Graph for inference or saving
    """
    if not tfjs_common.ARTIFACT_MODEL_TOPOLOGY_KEY in model_json:
        raise ValueError("model_json is missing key '{}'".format(
            tfjs_common.ARTIFACT_MODEL_TOPOLOGY_KEY))

    topology = model_json[tfjs_common.ARTIFACT_MODEL_TOPOLOGY_KEY]

    if not tfjs_common.ARTIFACT_WEIGHTS_MANIFEST_KEY in model_json:
        raise ValueError("model_json is missing key '{}'".format(
            tfjs_common.ARTIFACT_WEIGHTS_MANIFEST_KEY))

    weights_manifest = model_json[tfjs_common.ARTIFACT_WEIGHTS_MANIFEST_KEY]
    weight_list = read_weights(weights_manifest, base_path, flatten=True)

    graph_def = _convert_graph_def(topology)
    weight_dict = _convert_weight_list_to_dict(weight_list)
    with open("weights.pkl","wb") as f:
        pickle.dump(weight_dict,f)
    return _create_graph(graph_def, weight_dict)

def load_graph_model(model_dir):
    """
    Load a TFJS Graph Model from a directory

    Args:
        model_dir: Directory that contains the tfjs model.json and weights

    Returns:
        TF frozen graph for inference or saving
    """
    if not os.path.isdir(model_dir):
        raise ValueError("Model path is no directory: {}".format(model_dir))

    model_file_name = os.path.join(model_dir, tfjs_common.ARTIFACT_MODEL_JSON_FILE_NAME)
    if not os.path.exists(model_file_name) or not os.path.isfile(model_file_name):
        raise ValueError("Model not found: {}".format(model_file_name))

    with open(model_file_name, "r") as f:
        model_json = json.load(f)

    return _convert_graph_model_to_graph(model_json, model_dir)

def graph_model_to_frozen_graph(model_dir, export_path):
    """
    Convert a TFJS graph model to a frozen TF graph

    Args:
        model_dir: Directory that contains the TFJS JSON model and weights
        export_path: Path to the frozen graph (e.g. './output.pb')
    """
    export_dir = os.path.dirname(export_path)
    model_name = os.path.basename(export_path)

    graph = load_graph_model(model_dir)
    return tf.io.write_graph(graph, export_dir, model_name, as_text=False)

def graph_model_to_saved_model(model_dir, export_dir, tags):
    """
    Convert a TFJS graph model to a SavedModel

    Args:
        model_dir: Directory that contains the TFJS JSON model and weights
        export_dir: Target directory to save the TF model in
        tags: Tags for the SavedModel
    """
    graph = load_graph_model(model_dir)
    builder = tf.compat.v1.saved_model.Builder(export_dir)

    with tf.compat.v1.Session(graph=graph) as sess:
        builder.add_meta_graph_and_variables(sess, tags=tags)
    return builder.save()

def graph_models_to_saved_model(model_list, export_dir):
    """
    Reads multiple TFJS graph models and saves them in a single SavedModel

    Args:
        model_list: List of tuples containing TFJS model dir and tags, e.g.
            [("./models/model1", ["step1"]), ("./models/model2": ["step2"])]
        export_dir: Target directory to save the TF model in
    """
    builder = tf.compat.v1.saved_model.Builder(export_dir)

    model_dir, tags = model_list[0]
    graph = load_graph_model(model_dir)
    with tf.compat.v1.Session(graph=graph) as sess:
        builder.add_meta_graph_and_variables(sess, tags=tags)

    for model_dir, tags in model_list[1:]:
        graph = load_graph_model(model_dir)
        with tf.compat.v1.Session(graph=graph):
            builder.add_meta_graph(tags=tags)

    return builder.save()
