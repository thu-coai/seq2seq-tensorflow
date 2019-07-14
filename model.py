import numpy as np
import tensorflow as tf
import time

from tensorflow.python.ops.nn import dynamic_rnn
from utils.output_projection import output_projection_layer, MyDense
from utils import SummaryHelper


class Seq2SeqModel(object):
	def __init__(self, data, args, embed):

		self.posts = tf.placeholder(tf.int32, (None, None), 'enc_inps')  # batch*len
		self.posts_length = tf.placeholder(tf.int32, (None,), 'enc_lens')  # batch
		self.origin_responses = tf.placeholder(tf.int32, (None, None), 'dec_inps')  # batch*len
		self.origin_responses_length = tf.placeholder(tf.int32, (None,), 'dec_lens')  # batch

		# deal with original data to adapt encoder and decoder
		batch_size, decoder_len = tf.shape(self.origin_responses)[0], tf.shape(self.origin_responses)[1]
		self.responses = tf.split(self.origin_responses, [1, decoder_len-1], 1)[1] # no go_id
		self.responses_length = self.origin_responses_length - 1
		self.responses_input = tf.split(self.origin_responses, [decoder_len-1, 1], 1)[0] # no eos_id
		self.responses_target = self.responses
		decoder_len = decoder_len - 1
		self.posts_input = self.posts   # batch*len
		self.decoder_mask = tf.reshape(tf.cumsum(tf.one_hot(self.responses_length-1,
			decoder_len), reverse=True, axis=1), [-1, decoder_len])

		# initialize the training process
		self.learning_rate = tf.Variable(float(args.lr), trainable=False, dtype=tf.float32)
		self.learning_rate_decay_op = self.learning_rate.assign(self.learning_rate * args.lr_decay)
		self.global_step = tf.Variable(0, trainable=False)

		# build the embedding table and embedding input
		if embed is None:
			# initialize the embedding randomly
			self.embed = tf.get_variable('embed', [data.vocab_size, args.embedding_size], tf.float32)
		else:
			# initialize the embedding by pre-trained word vectors
			self.embed = tf.get_variable('embed', dtype=tf.float32, initializer=embed)
		
		self.encoder_input = tf.nn.embedding_lookup(self.embed, self.posts_input) #batch*len*unit
		self.decoder_input = tf.nn.embedding_lookup(self.embed, self.responses_input)

		# build rnn_cell
		cell_enc = tf.nn.rnn_cell.GRUCell(args.eh_size)
		cell_dec = tf.nn.rnn_cell.GRUCell(args.dh_size)

		# build encoder
		with tf.variable_scope('encoder'):
			encoder_output, encoder_state = dynamic_rnn(cell_enc, self.encoder_input,
				self.posts_length, dtype=tf.float32, scope="encoder_rnn")

		# get output projection function
		output_fn = MyDense(data.vocab_size, use_bias = True)
		sampled_sequence_loss = output_projection_layer(args.dh_size, data.vocab_size, args.softmax_samples)

		# construct helper and attention
		train_helper = tf.contrib.seq2seq.TrainingHelper(self.decoder_input, self.responses_length)
		infer_helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(self.embed, tf.fill([batch_size], data.go_id), data.eos_id)
		attn_mechanism = tf.contrib.seq2seq.LuongAttention(args.dh_size, encoder_output,
														   memory_sequence_length=self.posts_length)
		cell_dec_attn = tf.contrib.seq2seq.AttentionWrapper(cell_dec, attn_mechanism,
															attention_layer_size=args.dh_size)
		enc_state_shaping = tf.layers.dense(encoder_state, args.dh_size, activation = None)
		dec_start = cell_dec_attn.zero_state(batch_size, dtype = tf.float32).clone(cell_state = enc_state_shaping)

		# build decoder (train)
		with tf.variable_scope('decoder'):
			decoder_train = tf.contrib.seq2seq.BasicDecoder(cell_dec_attn, train_helper, dec_start)
			train_outputs, _, _ = tf.contrib.seq2seq.dynamic_decode(decoder_train, impute_finished = True, scope = "decoder_rnn")
			self.decoder_output = train_outputs.rnn_output
			self.decoder_distribution_teacher, self.decoder_loss = sampled_sequence_loss(self.decoder_output, self.responses_target, self.decoder_mask)

		# build decoder (test)
		with tf.variable_scope('decoder', reuse=True):
			decoder_infer = tf.contrib.seq2seq.BasicDecoder(cell_dec_attn, infer_helper, dec_start, output_layer = output_fn)
			infer_outputs, _, _ = tf.contrib.seq2seq.dynamic_decode(decoder_infer, impute_finished = True,
																 maximum_iterations=args.max_sent_length, scope = "decoder_rnn")
			self.decoder_distribution = infer_outputs.rnn_output
			self.generation_index = tf.argmax(tf.split(self.decoder_distribution,
				[2, data.vocab_size-2], 2)[1], 2) + 2 # for removing UNK

		# calculate the gradient of parameters and update
		self.params = [k for k in tf.trainable_variables() if args.name in k.name]
		opt = tf.train.AdamOptimizer(self.learning_rate)
		gradients = tf.gradients(self.decoder_loss, self.params)
		clipped_gradients, self.gradient_norm = tf.clip_by_global_norm(gradients, 
				args.grad_clip)
		self.update = opt.apply_gradients(zip(clipped_gradients, self.params), 
				global_step=self.global_step)

		# save checkpoint
		self.latest_saver = tf.train.Saver(write_version=tf.train.SaverDef.V2,
				max_to_keep=args.checkpoint_max_to_keep, pad_step_number=True, keep_checkpoint_every_n_hours=1.0)
		self.best_saver = tf.train.Saver(write_version=tf.train.SaverDef.V2,
				max_to_keep=1, pad_step_number=True, keep_checkpoint_every_n_hours=1.0)

		# create summary for tensorboard
		self.create_summary(args)

	def store_checkpoint(self, sess, path, key):
		if key == "latest":
			self.latest_saver.save(sess, path, global_step = self.global_step)
		else:
			self.best_saver.save(sess, path, global_step = self.global_step)
			#self.best_global_step = self.global_step

	def create_summary(self, args):
		self.summaryHelper = SummaryHelper("%s/%s_%s" % \
				(args.log_dir, args.name, time.strftime("%H%M%S", time.localtime())), args)

		self.trainSummary = self.summaryHelper.addGroup(scalar=["loss", "perplexity"], prefix="train")

		scalarlist = ["loss", "perplexity"]
		tensorlist = []
		textlist = []
		for i in args.show_sample:
			textlist.append("show_str%d" % i)
		self.devSummary = self.summaryHelper.addGroup(scalar=scalarlist, tensor=tensorlist, text=textlist,
													  prefix="dev")
		self.testSummary = self.summaryHelper.addGroup(scalar=scalarlist, tensor=tensorlist, text=textlist,
													  prefix="test")


	def print_parameters(self):
		for item in self.params:
			print('%s: %s' % (item.name, item.get_shape()))
	
	def step_decoder(self, session, data, forward_only=False):
		input_feed = {self.posts: data['post'], self.posts_length: data['post_length'],
				self.origin_responses: data['resp'], self.origin_responses_length: data['resp_length']}
		if forward_only:
			output_feed = [self.decoder_loss, self.decoder_distribution_teacher]
		else:
			output_feed = [self.decoder_loss, self.gradient_norm, self.update]
		return session.run(output_feed, input_feed)

	def inference(self, session, data):
		input_feed = {self.posts: data['post'], self.posts_length: data['post_length'],
					  self.origin_responses: data['resp'], self.origin_responses_length: data['resp_length']}
		output_feed = [self.generation_index]
		return session.run(output_feed, input_feed)

	def evaluate(self, sess, data, batch_size, key_name):
		loss = np.zeros((1,))
		times = 0
		data.restart(key_name, batch_size=batch_size, shuffle=False)
		batched_data = data.get_next_batch(key_name)
		while batched_data != None:
			outputs = self.step_decoder(sess, batched_data, forward_only=True)
			loss += outputs[0]
			times += 1
			batched_data = data.get_next_batch(key_name)
		loss /= times

		print('    perplexity on %s set: %.2f' % (key_name, np.exp(loss)))
		print(loss)
		return loss

	def train_process(self, sess, data, args):
		loss_step, time_step, epoch_step = np.zeros((1,)), .0, 0
		previous_losses = [1e18] * 5
		best_valid = 1e18
		data.restart("train", batch_size=args.batch_size, shuffle=True)
		batched_data = data.get_next_batch("train")
		for epoch_step in range(args.epochs):
			while batched_data != None:
				if self.global_step.eval() % args.checkpoint_steps == 0 and self.global_step.eval() != 0:
					show = lambda a: '[%s]' % (' '.join(['%.2f' % x for x in a]))
					print("Epoch %d global step %d learning rate %.4f step-time %.2f perplexity %s"
						  % (epoch_step, self.global_step.eval(), self.learning_rate.eval(),
							 time_step, show(np.exp(loss_step))))
					self.trainSummary(self.global_step.eval() // args.checkpoint_steps, {'loss': loss_step, 'perplexity': np.exp(loss_step)})
					#self.saver.save(sess, '%s/checkpoint_latest' % args.model_dir, global_step=self.global_step)\
					self.store_checkpoint(sess, '%s/checkpoint_latest/checkpoint' % args.model_dir, "latest")

					dev_loss = self.evaluate(sess, data, args.batch_size, "dev")
					self.devSummary(self.global_step.eval() // args.checkpoint_steps, {'loss': dev_loss, 'perplexity': np.exp(dev_loss)})

					test_loss = self.evaluate(sess, data, args.batch_size, "test")
					self.testSummary(self.global_step.eval() // args.checkpoint_steps, {'loss': test_loss, 'perplexity': np.exp(test_loss)})

					if np.sum(loss_step) > max(previous_losses):
						sess.run(self.learning_rate_decay_op)
					if dev_loss < best_valid:
						best_valid = dev_loss
						self.store_checkpoint(sess, '%s/checkpoint_best/checkpoint' % args.model_dir, "best")

					previous_losses = previous_losses[1:] + [np.sum(loss_step)]
					loss_step, time_step = np.zeros((1,)), .0

				start_time = time.time()
				loss_step += self.step_decoder(sess, batched_data)[0] / args.checkpoint_steps
				time_step += (time.time() - start_time) / args.checkpoint_steps
				batched_data = data.get_next_batch("train")

			data.restart("train", batch_size=args.batch_size, shuffle=True)
			batched_data = data.get_next_batch("train")

	def test_process(self, sess, data, args):
		metric1 = data.get_teacher_forcing_metric()
		metric2 = data.get_inference_metric()
		data.restart("test", batch_size=args.batch_size, shuffle=False)
		batched_data = data.get_next_batch("test")
		results = []
		while batched_data != None:
			batched_responses_id = self.inference(sess, batched_data)[0]
			_, gen_log_prob = self.step_decoder(sess, batched_data, forward_only=True)
			metric1_data = {'resp_allvocabs': np.array(batched_data['resp_allvocabs']), 'resp_length': np.array(batched_data['resp_length']),
							'gen_log_prob': np.array(gen_log_prob)}
			metric1.forward(metric1_data)
			batch_results = []
			for response_id in batched_responses_id:
				result_token = []
				response_id_list = response_id.tolist()
				response_token = data.convert_ids_to_tokens(response_id_list)
				if data.eos_id in response_id_list:
					result_id = response_id_list[:response_id_list.index(data.eos_id)+1]
				else:
					result_id = response_id_list
				for token in response_token:
					if token != data.ext_vocab[data.eos_id]:
						result_token.append(token)
					else:
						break
				results.append(result_token)
				batch_results.append(result_id)

			metric2_data = {'post_allvocabs': np.array(batched_data['post_allvocabs']), 'resp_allvocabs':np.array(batched_data['resp_allvocabs']),
							'gen': np.array(batch_results)}
			metric2.forward(metric2_data)
			batched_data = data.get_next_batch("test")

		res = metric1.close()
		res.update(metric2.close())

		test_file = args.out_dir + "/%s_%s.txt" % (args.name, "test")
		with open(test_file, 'w') as f:
			print("Test Result:")
			for key, value in res.items():
				if isinstance(value, float):
					print("\t%s:\t%f", key, value)
					f.write("%s:\t%f\n" % (key, value))
			for i in range(len(res['post'])):
				f.write("post:\t%s\n" % " ".join(res['post'][i]))
				f.write("resp:\t%s\n" % " ".join(res['resp'][i]))
				f.write("gen:\t%s\n" % " ".join(res['gen'][i]))

		print("result output to %s.", test_file)
		return {key: val for key, val in res.items() if type(val) in [bytes, int, float]}
