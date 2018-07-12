import torch
from torch.autograd import Variable
from torchvision import models
import cv2
import sys
import numpy as np

def replace_layers(model, i, indexes, layers):
	if i in indexes:
        # layers and indexes store new layers used to update old layers
		return layers[indexes.index(i)]
    # if i not in indexes, use old layers
	return model[i]

'''
Pruning function for VGGNet
Args:
    model: the model waiting for pruning
    (layer_index, filter_index): this locates the filter you want to prune within the model
Handle Cases: < : layers be affected
    1. Conv< + BN<
    2. Conv< + BN< + Pool + Conv<
    3. Conv< + BN< + Conv< + BN
    4. Tree paths
'''
def prune_vgg16_conv_layer(model, layer_index, filter_index):
	_, conv = model.features._modules.items()[layer_index]
	next_conv = None
	offset = 1
    # search for the next conv, based on current conv with id = (layer_index, filter_index)
	while layer_index + offset <  len(model.features._modules.items()):
		res =  model.features._modules.items()[layer_index+offset] # name, module
		if isinstance(res[1], torch.nn.modules.conv.Conv2d):
			next_name, next_conv = res
			break
        # TODO: weights of Batch Normalization layer need to be removed
		offset = offset + 1

    # the updated conv for current conv, with 1 output channel being pruned
    # nothing else change
	new_conv = \
		torch.nn.Conv2d(in_channels = conv.in_channels, \
			out_channels = conv.out_channels - 1,
			kernel_size = conv.kernel_size, \
			stride = conv.stride,
			padding = conv.padding,
			dilation = conv.dilation,
			groups = conv.groups,
			bias = conv.bias) #(out_channels)

	old_weights = conv.weight.data.cpu().numpy() # (out_channels, in_channels, kernel_size[0], kernel_size[1]
	new_weights = new_conv.weight.data.cpu().numpy()

    # skip that filter's weight inside old_weights and store others into new_weights
	new_weights[: filter_index, :, :, :] = old_weights[: filter_index, :, :, :] # [0, filter_index)
	new_weights[filter_index : , :, :, :] = old_weights[filter_index + 1 :, :, :, :] # [filter_index + 1, end]
	new_conv.weight.data = torch.from_numpy(new_weights).cuda()

	bias_numpy = conv.bias.data.cpu().numpy()

    # change size to (out_channels - 1)
	bias = np.zeros(shape = (bias_numpy.shape[0] - 1), dtype = np.float32)
	bias[:filter_index] = bias_numpy[:filter_index]
	bias[filter_index : ] = bias_numpy[filter_index + 1 :]
	new_conv.bias.data = torch.from_numpy(bias).cuda()

    # next_conv exists
	if not next_conv is None:
		next_new_conv = \
			torch.nn.Conv2d(in_channels = next_conv.in_channels - 1,\
				out_channels =  next_conv.out_channels, \
				kernel_size = next_conv.kernel_size, \
				stride = next_conv.stride,
				padding = next_conv.padding,
				dilation = next_conv.dilation,
				groups = next_conv.groups,
				bias = next_conv.bias)

		old_weights = next_conv.weight.data.cpu().numpy()
		new_weights = next_new_conv.weight.data.cpu().numpy()

		new_weights[:, : filter_index, :, :] = old_weights[:, : filter_index, :, :] # (out_channels, in_channels, kernel_size[0], kernel_size[1]
		new_weights[:, filter_index : , :, :] = old_weights[:, filter_index + 1 :, :, :]
		next_new_conv.weight.data = torch.from_numpy(new_weights).cuda()

		next_new_conv.bias.data = next_conv.bias.data

	if not next_conv is None:
        # replace current layer and next_conv with new_conv and next_new_conv respectively
	 	features = torch.nn.Sequential(
	            *(replace_layers(model.features, i, [layer_index, layer_index+offset], \
	            	[new_conv, next_new_conv]) for i, _ in enumerate(model.features)))
	 	del model.features # delete and replace with brand new one
	 	del conv

	 	model.features = features

	else:
		#Prunning the last conv layer. This affects the first linear layer of the classifier.
	 	model.features = torch.nn.Sequential(
	            *(replace_layers(model.features, i, [layer_index], \
	            	[new_conv]) for i, _ in enumerate(model.features)))
	 	layer_index = 0
	 	old_linear_layer = None
	 	for _, module in model.classifier._modules.items():
	 		if isinstance(module, torch.nn.Linear):
	 			old_linear_layer = module
	 			break
	 		layer_index = layer_index  + 1

	 	if old_linear_layer is None:
	 		raise BaseException("No linear layer found in classifier")
		params_per_input_channel = old_linear_layer.in_features / conv.out_channels

	 	new_linear_layer = \
	 		torch.nn.Linear(old_linear_layer.in_features - params_per_input_channel,
	 			old_linear_layer.out_features)

	 	old_weights = old_linear_layer.weight.data.cpu().numpy()
	 	new_weights = new_linear_layer.weight.data.cpu().numpy()

	 	new_weights[:, : filter_index * params_per_input_channel] = \
	 		old_weights[:, : filter_index * params_per_input_channel]
	 	new_weights[:, filter_index * params_per_input_channel :] = \
	 		old_weights[:, (filter_index + 1) * params_per_input_channel :]

	 	new_linear_layer.bias.data = old_linear_layer.bias.data

	 	new_linear_layer.weight.data = torch.from_numpy(new_weights).cuda()

		classifier = torch.nn.Sequential(
			*(replace_layers(model.classifier, i, [layer_index], \
				[new_linear_layer]) for i, _ in enumerate(model.classifier)))

		del model.classifier
		del next_conv
		del conv
		model.classifier = classifier

	return model

'''
Pruning function for VGG backbone in SSD/RefineDet
Args:
    model: the model waiting for pruning
    (layer_index, filter_index): this locates the filter you want to prune within the model
Handle Cases: < : layers be affected
    1. Conv< + BN<
    2. Conv< + BN< + Pool + Conv<
    3. Conv< + BN< + Conv< + BN
    4. Tree paths
'''
def prune_vggbase_conv_layer(model, layer_index, filter_index):
	_, conv = model.features._modules.items()[layer_index]
	next_conv = None
	offset = 1
    # search for the next conv, based on current conv with id = (layer_index, filter_index)
	while layer_index + offset <  len(model.features._modules.items()):
		res =  model.features._modules.items()[layer_index+offset] # name, module
		if isinstance(res[1], torch.nn.modules.conv.Conv2d):
			next_name, next_conv = res
			break
        # TODO: weights of Batch Normalization layer need to be removed
		offset = offset + 1

    # the updated conv for current conv, with 1 output channel being pruned
    # nothing else change
	new_conv = \
		torch.nn.Conv2d(in_channels = conv.in_channels, \
			out_channels = conv.out_channels - 1,
			kernel_size = conv.kernel_size, \
			stride = conv.stride,
			padding = conv.padding,
			dilation = conv.dilation,
			groups = conv.groups,
			bias = conv.bias) #(out_channels)

	old_weights = conv.weight.data.cpu().numpy() # (out_channels, in_channels, kernel_size[0], kernel_size[1]
	new_weights = new_conv.weight.data.cpu().numpy()

    # skip that filter's weight inside old_weights and store others into new_weights
	new_weights[: filter_index, :, :, :] = old_weights[: filter_index, :, :, :] # [0, filter_index)
	new_weights[filter_index : , :, :, :] = old_weights[filter_index + 1 :, :, :, :] # [filter_index + 1, end]
	new_conv.weight.data = torch.from_numpy(new_weights).cuda()

	bias_numpy = conv.bias.data.cpu().numpy()

    # change size to (out_channels - 1)
	bias = np.zeros(shape = (bias_numpy.shape[0] - 1), dtype = np.float32)
	bias[:filter_index] = bias_numpy[:filter_index]
	bias[filter_index : ] = bias_numpy[filter_index + 1 :]
	new_conv.bias.data = torch.from_numpy(bias).cuda()

    # next_conv exists
	if not next_conv is None:
		next_new_conv = \
			torch.nn.Conv2d(in_channels = next_conv.in_channels - 1,\
				out_channels =  next_conv.out_channels, \
				kernel_size = next_conv.kernel_size, \
				stride = next_conv.stride,
				padding = next_conv.padding,
				dilation = next_conv.dilation,
				groups = next_conv.groups,
				bias = next_conv.bias)

		old_weights = next_conv.weight.data.cpu().numpy()
		new_weights = next_new_conv.weight.data.cpu().numpy()

		new_weights[:, : filter_index, :, :] = old_weights[:, : filter_index, :, :] # (out_channels, in_channels, kernel_size[0], kernel_size[1]
		new_weights[:, filter_index : , :, :] = old_weights[:, filter_index + 1 :, :, :]
		next_new_conv.weight.data = torch.from_numpy(new_weights).cuda()

		next_new_conv.bias.data = next_conv.bias.data

	if not next_conv is None:
        # replace current layer and next_conv with new_conv and next_new_conv respectively
	 	features = torch.nn.Sequential(
	            *(replace_layers(model.features, i, [layer_index, layer_index+offset], \
	            	[new_conv, next_new_conv]) for i, _ in enumerate(model.features)))
	 	del model.features # delete and replace with brand new one
	 	del conv

	 	model.features = features

	else:
		#Prunning the last conv layer. This affects the first linear layer of the classifier.
	 	model.features = torch.nn.Sequential(
	            *(replace_layers(model.features, i, [layer_index], \
	            	[new_conv]) for i, _ in enumerate(model.features)))
	 	layer_index = 0
	 	old_linear_layer = None
	 	for _, module in model.classifier._modules.items():
	 		if isinstance(module, torch.nn.Linear):
	 			old_linear_layer = module
	 			break
	 		layer_index = layer_index  + 1

	 	if old_linear_layer is None:
	 		raise BaseException("No linear layer found in classifier")
		params_per_input_channel = old_linear_layer.in_features / conv.out_channels

	 	new_linear_layer = \
	 		torch.nn.Linear(old_linear_layer.in_features - params_per_input_channel,
	 			old_linear_layer.out_features)

	 	old_weights = old_linear_layer.weight.data.cpu().numpy()
	 	new_weights = new_linear_layer.weight.data.cpu().numpy()

	 	new_weights[:, : filter_index * params_per_input_channel] = \
	 		old_weights[:, : filter_index * params_per_input_channel]
	 	new_weights[:, filter_index * params_per_input_channel :] = \
	 		old_weights[:, (filter_index + 1) * params_per_input_channel :]

	 	new_linear_layer.bias.data = old_linear_layer.bias.data

	 	new_linear_layer.weight.data = torch.from_numpy(new_weights).cuda()

		classifier = torch.nn.Sequential(
			*(replace_layers(model.classifier, i, [layer_index], \
				[new_linear_layer]) for i, _ in enumerate(model.classifier)))

		del model.classifier
		del next_conv
		del conv
		model.classifier = classifier

	return model

'''
# pruning demo
if __name__ == '__main__':
	model = models.vgg16(pretrained=True)
	model.train()

	t0 = time.time()
	model = prune_conv_layer(model, 28, 10)
	print "The prunning took", time.time() - t0
'''
