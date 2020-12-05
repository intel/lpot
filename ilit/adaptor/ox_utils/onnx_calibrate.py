#!/usr/bin/env python
# coding: utf-8
# -------------------------------------------------------------------------
# Copyright (c) Microsoft, Intel Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------


import abc
import copy
import logging

import numpy as np
import onnx
import onnxruntime
from onnxruntime.quantization.registry import CreateDefaultOpQuantizer, IntegerOpsRegistry, \
    QLinearOpsRegistry
from onnxruntime.quantization.operators.base_operator import QuantOperatorBase
from onnxruntime.quantization.quant_utils import __producer__, __version__, attribute_to_kwarg, \
    QuantizationMode, QuantizedValue, QuantizedValueType
from onnx import helper, TensorProto
from onnx import onnx_pb as onnx_proto

logger = logging.getLogger()

class CalibrationDataReader(metaclass=abc.ABCMeta):
    @classmethod
    def __subclasshook__(cls, subclass):
        return (hasattr(subclass, 'get_next') and callable(subclass.get_next) or NotImplemented)

    @abc.abstractmethod
    def get_next(self) -> dict:
        """generate the input data dict for ONNXinferenceSession run"""
        raise NotImplementedError


class ONNXCalibrater:
    def __init__(self, model, data_reader: CalibrationDataReader, calibrate_op_types, black_nodes,
                 white_nodes, augmented_model_path, iterations=1):
        '''
        :param model: ONNX model to calibrate
        :param data_reader: user implemented object to read in and preprocess calibration dataset
                            based on CalibrationDataReader Interface
        :param op_types: operator types to be calibrated and quantized, default = 'Conv,MatMul'
        :param black_nodes: operator names that should not be quantized, default = ''
        :param white_nodes: operator names that force to be quantized, default = ''
        :param augmented_model_path: save augmented_model to this path

        '''
        self.model = model
        self.data_reader = data_reader
        self.calibrate_op_types = calibrate_op_types
        self.black_nodes = black_nodes
        self.white_nodes = white_nodes
        self.augmented_model_path = augmented_model_path
        self.input_name_to_nodes = {}
        self.iterations = iterations

    def augment_graph(self):
        '''
        Adds ReduceMin and ReduceMax nodes to all quantization_candidates op type nodes in
        model and ensures their outputs are stored as part of the graph output
        :return: augmented ONNX model
        '''

        model = copy.deepcopy(self.model)

        added_nodes = []
        added_outputs = []
        tensors_to_calibrate = set()

        for node in model.graph.node:
            should_be_calibrate = ((node.op_type in self.calibrate_op_types) and
                                   (node.name not in self.black_nodes)) or \
                                   (node.name in self.white_nodes)
            if should_be_calibrate:
                if node.op_type == "Attention":
                    logger.debug("indice input {} of attention node {} can't be calibrated"
                                 .format(node.input[-1], node.name))
                    tensors_to_calibrate.update(node.input[:-1])
                else:
                    tensors_to_calibrate.update(node.input)
                tensors_to_calibrate.update(node.output)

            for tensor in tensors_to_calibrate:
                if tensor in model.graph.initializer:
                    tensors_to_calibrate.remove(tensor)

        for tensor in tensors_to_calibrate:
            # Adding ReduceMin nodes
            reduce_min_name = tensor + '_ReduceMin'
            reduce_min_node = onnx.helper.make_node('ReduceMin', [tensor], [tensor + '_ReduceMin'],
                                                    reduce_min_name,
                                                    keepdims=0)

            added_nodes.append(reduce_min_node)
            added_outputs.append(helper.make_tensor_value_info(reduce_min_node.output[0],
                                                               TensorProto.FLOAT, ()))

            # Adding ReduceMax nodes
            reduce_max_name = tensor + '_ReduceMax'
            reduce_max_node = onnx.helper.make_node('ReduceMax', [tensor], [tensor + '_ReduceMax'],
                                                    reduce_max_name,
                                                    keepdims=0)

            added_nodes.append(reduce_max_node)
            added_outputs.append(helper.make_tensor_value_info(reduce_max_node.output[0],
                                                               TensorProto.FLOAT, ()))

        model.graph.node.extend(added_nodes)
        model.graph.output.extend(added_outputs)

        return model

    # Using augmented outputs to generate inputs for quantization
    def get_intermediate_outputs(self, calib_mode='naive'):
        '''
            Gather intermediate model outputs after running inference
            parameter calib_mode: type 'naive' gives (ReduceMin, ReduceMax) pairs
                                for each augmented node across test data sets, where
                                the first element is a minimum of all ReduceMin values
                                and the second element is a maximum of all ReduceMax
                                values;
            :return: dictionary mapping: {added node names: (ReduceMin, ReduceMax) pairs }
        '''

        # conduct inference session and get intermediate outputs
        session = onnxruntime.InferenceSession(self.augmented_model_path, None)

        intermediate_outputs = []

        for idx, batch in enumerate(self.data_reader):
            # batch = tuple(t.detach().cpu().numpy() for t in batch)
            ort_inputs = {}
            for i in range(len(session.get_inputs())):
                ort_inputs.update({session.get_inputs()[i].name: batch[i]})
            intermediate_outputs.append(session.run(None, ort_inputs))
            if idx >= self.iterations - 1:
                break
        node_output_names = [session.get_outputs()[i].name for i in
                             range(len(intermediate_outputs[0]))]
        output_dicts_list = [
            dict(zip(node_output_names, intermediate_output)) for intermediate_output in
            intermediate_outputs
        ]

        # number of outputs in original model
        model = self.model
        num_model_outputs = len(model.graph.output)
        merged_dict = {}
        for d in output_dicts_list:
            for k, v in d.items():
                merged_dict.setdefault(k, []).append(v)
        added_node_output_names = node_output_names[num_model_outputs:]
        node_names = [added_node_output_names[i].rpartition('_')[0]
                      for i in range(0, len(added_node_output_names), 2)]  # output names

        # Characterizing distribution of a node's values across test data sets
        clean_merged_dict = dict((i, merged_dict[i]) for i in merged_dict
                                 if i != list(merged_dict.keys())[0])
        if calib_mode == 'naive':
            pairs = [
                tuple([
                    float(min(clean_merged_dict[added_node_output_names[i]])),
                    float(max(clean_merged_dict[added_node_output_names[i + 1]]))
                ]) for i in range(0, len(added_node_output_names), 2)
            ]
        else:
            raise ValueError('Unknown value for calib_mode. \
                             Currently only naive mode is supported.')

        final_dict = dict(zip(node_names, pairs))

        return final_dict

    def _get_input_name_to_nodes(self, model):
        '''
            Helper function to get input_name_to_nodes dictionary
        '''

        for node in model.graph.node:
            for input_name in node.input:
                if input_name not in self.input_name_to_nodes:
                    self.input_name_to_nodes[input_name] = [node]
                else:
                    self.input_name_to_nodes[input_name].append(node)

    def calculate_scale_zeropoint(self, next_node, rmin, rmax):

        zp_and_scale = []
        # adjust rmin and rmax such that 0 is included in the range. This is required
        # to make sure zero can be uniquely represented.
        rmin = min(rmin, 0)
        rmax = max(rmax, 0)
        # We update the output range min and max when next node is clip or relu
        # With this technique we can remove these 2 ops and
        # reduce the output range which in turn helps to improve accuracy
        if next_node:
            if next_node.op_type == 'Clip':
                for attr in next_node.attribute:
                    if attr.name == 'max':
                        clip_max = attr.f
                    elif attr.name == 'min':
                        clip_min = attr.f
                if rmin < clip_min:
                    rmin = clip_min
                if rmax > clip_max:
                    rmax = clip_max
            elif next_node.op_type == 'Relu':
                if rmin < 0:
                    rmin = 0

        scale = np.float32((rmax - rmin) / 255 if rmin != rmax else 1)
        initial_zero_point = (0 - rmin) / scale
        zero_point = np.uint8(round(max(0, min(255, initial_zero_point))))

        zp_and_scale.append(zero_point)
        zp_and_scale.append(scale)

        return zp_and_scale

    def calculate_quantization_params(self, quantization_thresholds):
        '''
            Given quantization thresholds, calculate the quantization params.
        :param quantization_thresholds:
            Dictionary specifying the min and max values for outputs of conv and matmul nodes.
            The quantization_thresholds should be specified in the following format:
                {
                    "param_name": [min, max]
                }
            example:
                {
                    'Conv_3:0': [np.float32(0), np.float32(0.5)],
                    'Conv_4:0': [np.float32(1), np.float32(3.5)]
                }
        :return: Dictionary containing the zero point and
                  scale values for outputs of conv and matmul nodes.
            The dictionary format is
                {
                    "param_name": [zero_point, scale]
                }
        '''
        if quantization_thresholds is None:
            raise ValueError(
                'quantization thresholds is required to calculate quantization \
                    params (zero point and scale)')

        quantization_params = {}
        model = self.model

        self._get_input_name_to_nodes(model)

        for tensor_name in quantization_thresholds.keys():
            child = None
            if tensor_name in self.input_name_to_nodes:
                children = self.input_name_to_nodes[tensor_name]
                if (len(children) == 1):
                    child = children[0]
            node_thresholds = quantization_thresholds[tensor_name]
            node_params = self.calculate_scale_zeropoint(child, node_thresholds[0],
                                                         node_thresholds[1])
            quantization_params[tensor_name] = node_params

        return quantization_params


def calibrate(model,
              data_reader: CalibrationDataReader,
              op_types=['Conv', 'MatMul'],
              black_nodes=[],
              white_nodes=[],
              augmented_model_path='augmented_model.onnx',
              iterations=1):
    '''
        Given an onnx model, augment and run the augmented model on calibration data set, \
              aggregate and calculate the quantization parameters.

    :param model: ONNX model to calibrate
    :param data_reader: user implemented object to read in and preprocess calibration dataset \
        based on CalibrationDataReader interface
    :param op_types: operator types to be calibrated and quantized, default = 'Conv,MatMul'
    :param black_nodes: operator names that should not be quantized, default = ''
    :param white_nodes: operator names that force to be quantized, default = ''
    :param augmented_model_path: save augmented_model to this path
    '''
    # 1. initialize a calibrater
    calibrater = ONNXCalibrater(model, data_reader, op_types, black_nodes, white_nodes,
                                augmented_model_path, iterations)
    # 2. augment
    augmented_model = calibrater.augment_graph()
    onnx.save(augmented_model, augmented_model_path)
    # 3. generate quantization thresholds
    dict_for_quantization = calibrater.get_intermediate_outputs()
    # 4. generate quantization parameters dict
    quantization_params_dict = calibrater.calculate_quantization_params(dict_for_quantization)

    print("Calibrated,quantized parameters calculated and returned.")
    return quantization_params_dict

