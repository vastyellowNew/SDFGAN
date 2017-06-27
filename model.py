from __future__ import division
import time
from glob import glob

from ops import *
from utils import *


def conv_out_size_same(size, stride):
    return int(math.ceil(float(size) / float(stride)))


class SDFGAN(object):
    def __init__(self, sess, input_depth=64, input_height=64, input_width=64, is_crop=True,
                 batch_size=64, sample_num=64,
                 output_depth=64, output_height=64, output_width=64, z_dim=200, gf_dim=64, df_dim=64,
                 gfc_dim=1024, dfc_dim=1024, c_dim=1, dataset_name='shapenet',
                 input_fname_pattern='*.npy', checkpoint_dir=None, dataset_dir=None, log_dir=None, sample_dir=None,
                 num_gpus=1, field_constraint=0.1):
        """

        Args:
          sess: TensorFlow session
          batch_size: The size of batch. Should be specified before training.
          z_dim: (optional) Dimension of dim for Z. [100]
          gf_dim: (optional) Dimension of gen filters in first conv layer. [64]
          df_dim: (optional) Dimension of discrim filters in first conv layer. [64]
          gfc_dim: (optional) Dimension of gen units for for fully connected layer. [1024]
          dfc_dim: (optional) Dimension of discrim units for fully connected layer. [1024]
          c_dim: (optional) Dimension of image color. For grayscale input, set to 1. [3]
        """
        self.sess = sess
        self.is_crop = is_crop
        self.is_grayscale = (c_dim == 1)

        self.batch_size = batch_size
        self.sample_num = sample_num

        self.input_depth = input_depth
        self.input_height = input_height
        self.input_width = input_width

        self.output_depth = output_depth
        self.output_height = output_height
        self.output_width = output_width

        self.z_dim = z_dim

        self.gf_dim = gf_dim
        self.df_dim = df_dim

        self.gfc_dim = gfc_dim
        self.dfc_dim = dfc_dim

        self.c_dim = c_dim
        self.num_gpus = num_gpus
        self.glob_batch_size = self.num_gpus * self.batch_size
        self.field_constraint = field_constraint

        # batch normalization : deals with poor initialization helps gradient flow
        self.d_bn1 = batch_norm(name='d_bn1')
        self.d_bn2 = batch_norm(name='d_bn2')
        self.d_bn3 = batch_norm(name='d_bn3')

        self.g_bn0 = batch_norm(name='g_bn0')
        self.g_bn1 = batch_norm(name='g_bn1')
        self.g_bn2 = batch_norm(name='g_bn2')
        self.g_bn3 = batch_norm(name='g_bn3')

        self.dataset_name = dataset_name
        self.input_fname_pattern = input_fname_pattern
        self.checkpoint_dir = checkpoint_dir
        self.dataset_dir = dataset_dir
        self.log_dir = log_dir
        self.sample_dir = sample_dir
        self.build_model()

    def build_model(self):

        if self.is_crop:
            image_dims = [self.output_depth, self.output_height, self.output_width, self.c_dim]
        else:
            image_dims = [self.output_depth, self.input_height, self.input_width, self.c_dim]

        # input placeholders
        self.inputs = tf.placeholder(
            tf.float32, [self.glob_batch_size] + image_dims, name='real_images')
        self.z = tf.placeholder(
            tf.float32, [None, self.z_dim], name='z')
        self.n_eff = tf.placeholder(tf.int32, name='n_eff')  # overall number of effective data points

        self.z_sum = histogram_summary("z", self.z)

        # initialize global lists
        self.G = [None] * self.num_gpus
        self.D = [None] * self.num_gpus
        self.D_logits = [None] * self.num_gpus
        self.D_ = [None] * self.num_gpus
        self.D_logits_ = [None] * self.num_gpus
        self.d_loss_real = [None] * self.num_gpus
        self.d_loss_fake = [None] * self.num_gpus
        self.d_losses = [None] * self.num_gpus
        self.g_losses = [None] * self.num_gpus
        self.d_accus = [None] * self.num_gpus
        self.n_effs = [None] * self.num_gpus
        self.g_loss_eik = [None] * self.num_gpus
        self.g_loss_sym = [None] * self.num_gpus
        self.g_loss_gen = [None] * self.num_gpus

        # compute using multiple gpus
        with tf.variable_scope(tf.get_variable_scope()) as vscope:
            for gpuid in xrange(self.num_gpus):
                with tf.device('/gpu:%d' % gpuid):

                    # range of data for this gpu
                    gpu_start = gpuid * self.batch_size
                    gpu_end = (gpuid + 1) * self.batch_size

                    # number of effective data points
                    gpu_n_eff = tf.reduce_min([tf.reduce_max([0, self.n_eff - gpu_start]), self.batch_size])

                    # create examples and pass through discriminator
                    gpu_G = self.generator(self.z[gpu_start:gpu_end])
                    gpu_D, gpu_D_logits = self.discriminator(self.inputs[gpu_start:gpu_end])
                    gpu_D_, gpu_D_logits_ = self.discriminator(gpu_G, reuse=True)

                    # compatibility across different tf versions
                    def sigmoid_cross_entropy_with_logits(x, y):
                        try:
                            return tf.nn.sigmoid_cross_entropy_with_logits(logits=x, labels=y)
                        except:
                            return tf.nn.sigmoid_cross_entropy_with_logits(logits=x, targets=y)

                    # compute loss and accuracy
                    gpu_d_loss_real = tf.reduce_mean(
                        sigmoid_cross_entropy_with_logits(gpu_D_logits[:gpu_n_eff], tf.ones_like(gpu_D[:gpu_n_eff])))
                    gpu_d_loss_fake = tf.reduce_mean(
                        sigmoid_cross_entropy_with_logits(gpu_D_logits_[:gpu_n_eff], tf.zeros_like(gpu_D_[:gpu_n_eff])))
                    gpu_g_loss_gen = tf.reduce_mean(
                        sigmoid_cross_entropy_with_logits(gpu_D_logits_[:gpu_n_eff], tf.ones_like(gpu_D_[:gpu_n_eff])))
                    gpu_d_loss = gpu_d_loss_real + gpu_d_loss_fake
                    gpu_d_accu_real = tf.reduce_sum(tf.cast(gpu_D[:gpu_n_eff] > .5, tf.int32)) / gpu_D.get_shape()[0]
                    gpu_d_accu_fake = tf.reduce_sum(tf.cast(gpu_D_[:gpu_n_eff] < .5, tf.int32)) / gpu_D_.get_shape()[0]
                    gpu_d_accu = (gpu_d_accu_real + gpu_d_accu_fake) / 2

                    # compute generator field constraint loss
                    # enforce eikonal equation
                    delta_d, delta_h, delta_w = 1 / (np.array(image_dims[:-1]) - 1)
                    gpu_G_d1, gpu_G_d0 = gpu_G[2:, 1:-1, 1:-1], gpu_G[:-2, 1:-1, 1:-1]
                    gpu_G_h1, gpu_G_h0 = gpu_G[1:-1, 2:, 1:-1], gpu_G[1:-1, :-2, 1:-1]
                    gpu_G_w1, gpu_G_w0 = gpu_G[1:-1, 1:-1, 2:], gpu_G[1:-1, 1:-1, :-2]
                    grad_G_d = tf.expand_dims((gpu_G_d1 - gpu_G_d0) / 2 / delta_d, axis=0)
                    grad_G_h = tf.expand_dims((gpu_G_h1 - gpu_G_h0) / 2 / delta_h, axis=0)
                    grad_G_w = tf.expand_dims((gpu_G_w1 - gpu_G_w0) / 2 / delta_w, axis=0)
                    grad_G = tf.concat([grad_G_d, grad_G_h, grad_G_w], axis=0)
                    grad_G_norms = tf.norm(grad_G, axis=0)
                    grad_G_diff2 = tf.square(tf.reduce_mean(grad_G_norms)-tf.ones_like(grad_G_norms))
                    gpu_g_loss_eik = tf.abs(tf.reduce_mean(grad_G_diff2))

                    # enforce symmetry (along dim=2)
                    gpu_left = gpu_G[:, :, :int(image_dims[2] / 2)]
                    gpu_rite = tf.reverse(gpu_G[:, :, int(image_dims[2] / 2):], axis=[2])
                    gpu_g_loss_sym = tf.reduce_mean(tf.square(tf.subtract(gpu_left, gpu_rite)))

                    # combined generator loss
                    gpu_g_loss = gpu_g_loss_gen + (gpu_g_loss_eik + gpu_g_loss_sym) * self.field_constraint

                    # add gpu-wise data to global list
                    self.G[gpuid] = gpu_G
                    self.D[gpuid] = gpu_D
                    self.D_[gpuid] = gpu_D_
                    self.D_logits[gpuid] = gpu_D_logits
                    self.D_logits_[gpuid] = gpu_D_logits_
                    self.d_loss_real[gpuid] = gpu_d_loss_real
                    self.d_loss_fake[gpuid] = gpu_d_loss_fake
                    self.d_losses[gpuid] = gpu_d_loss
                    self.g_loss_eik[gpuid] = gpu_g_loss_eik
                    self.g_loss_sym[gpuid] = gpu_g_loss_sym
                    self.g_loss_gen[gpuid] = gpu_g_loss_gen
                    self.g_losses[gpuid] = gpu_g_loss
                    self.d_accus[gpuid] = gpu_d_accu
                    self.n_effs[gpuid] = gpu_n_eff

                    # Reuse variables for the next gpu
                    tf.get_variable_scope().reuse_variables()

        # concatenate across GPUs
        self.D = tf.concat(self.D, axis=0)
        self.D_ = tf.concat(self.D_, axis=0)
        self.G = tf.concat(self.G, axis=0)
        weighted_d_loss_real = [self.d_loss_real[j] * tf.cast(self.n_effs[j], tf.float32)
                                / tf.cast(self.n_eff, tf.float32) for j in range(self.num_gpus)]
        weighted_d_loss_fake = [self.d_loss_fake[j] * tf.cast(self.n_effs[j], tf.float32)
                                / tf.cast(self.n_eff, tf.float32) for j in range(self.num_gpus)]
        weighted_d_loss = [self.d_losses[j] * tf.cast(self.n_effs[j], tf.float32)
                           / tf.cast(self.n_eff, tf.float32) for j in range(self.num_gpus)]
        weighted_g_loss_eik = [tf.cast(self.g_loss_eik[j], tf.float32) * tf.cast(self.n_effs[j], tf.float32)
                                 / tf.cast(self.n_eff, tf.float32) for j in range(self.num_gpus)]
        weighted_g_loss_sym = [tf.cast(self.g_loss_sym[j], tf.float32) * tf.cast(self.n_effs[j], tf.float32)
                                 / tf.cast(self.n_eff, tf.float32) for j in range(self.num_gpus)]
        weighted_g_loss_gen = [tf.cast(self.g_loss_gen[j], tf.float32) * tf.cast(self.n_effs[j], tf.float32)
                                 / tf.cast(self.n_eff, tf.float32) for j in range(self.num_gpus)]
        weighted_g_loss = [self.g_losses[j] * tf.cast(self.n_effs[j], tf.float32)
                           / tf.cast(self.n_eff, tf.float32) for j in range(self.num_gpus)]
        weighted_d_accu = [tf.cast(self.d_accus[j], tf.float32) * tf.cast(self.n_effs[j], tf.float32)
                           / tf.cast(self.n_eff, tf.float32) for j in range(self.num_gpus)]

        self.d_loss_real = tf.reduce_sum(weighted_d_loss_real, axis=0)
        self.d_loss_fake = tf.reduce_sum(weighted_d_loss_fake, axis=0)
        self.d_loss = tf.reduce_sum(weighted_d_loss, axis=0)
        self.g_loss_eik = tf.reduce_sum(weighted_g_loss_eik, axis=0)
        self.g_loss_sym = tf.reduce_sum(weighted_g_loss_sym, axis=0)
        self.g_loss_gen = tf.reduce_sum(weighted_g_loss_gen, axis=0)
        self.g_loss = tf.reduce_sum(weighted_g_loss, axis=0)
        self.d_accu = tf.reduce_sum(weighted_d_accu, axis=0)

        # summarize variables
        self.d_sum = histogram_summary("d", self.D)
        self.d__sum = histogram_summary("d_", self.D_)
        self.g_sum = image_summary("G", self.G[:, 32, :, :])

        self.d_loss_real_sum = scalar_summary("d_loss_real", self.d_loss_real)
        self.d_loss_fake_sum = scalar_summary("d_loss_fake", self.d_loss_fake)
        self.d_loss_sum = scalar_summary("d_loss", self.d_loss)

        self.g_loss_eik_sum = scalar_summary("g_loss_eik", self.g_loss_eik)
        self.g_loss_sym_sum = scalar_summary("g_loss_sym", self.g_loss_sym)
        self.g_loss_gen_sum = scalar_summary("g_loss_gen", self.g_loss_gen)
        self.g_loss_sum = scalar_summary("g_loss", self.g_loss)
        self.d_accu_sum = scalar_summary("d_accu", self.d_accu)

        # define trainable variables for generator and discriminator
        t_vars = tf.trainable_variables()
        self.d_vars = [var for var in t_vars if 'd_' in var.name]
        self.g_vars = [var for var in t_vars if 'g_' in var.name]

        self.sampler = self.sampler(self.z)
        self.saver = tf.train.Saver()

    def train(self, config):
        """Train SFDGAN"""
        data = glob(os.path.join(self.dataset_dir, config.dataset, self.input_fname_pattern))
        np.random.shuffle(data)

        # define optimization operation
        d_opt = tf.train.AdamOptimizer(config.d_learning_rate, beta1=config.beta1)
        g_opt = tf.train.AdamOptimizer(config.g_learning_rate, beta1=config.beta1)

        # create list of grads from different gpus
        global_d_grads_vars = [None] * self.num_gpus
        global_g_grads_vars = [None] * self.num_gpus

        # compute d gradients
        with tf.variable_scope(tf.get_variable_scope()):
            for gpuid in xrange(self.num_gpus):
                with tf.device('/gpu:%d' % gpuid):
                    gpu_d_grads_vars = d_opt.compute_gradients(loss=self.d_losses[gpuid], var_list=self.d_vars)
                    global_d_grads_vars[gpuid] = gpu_d_grads_vars

        # compute g gradients
        with tf.variable_scope(tf.get_variable_scope()):
            for gpuid in xrange(self.num_gpus):
                with tf.device('/gpu:%d' % gpuid):
                    gpu_g_grads_vars = g_opt.compute_gradients(loss=self.g_losses[gpuid], var_list=self.g_vars)
                    global_g_grads_vars[gpuid] = gpu_g_grads_vars

        # average gradients across gpus and apply gradients
        d_grads_vars = average_gradients(global_d_grads_vars)
        g_grads_vars = average_gradients(global_g_grads_vars)
        d_optim = d_opt.apply_gradients(d_grads_vars)
        g_optim = g_opt.apply_gradients(g_grads_vars)

        # compatibility across tf versions
        try:
            tf.global_variables_initializer().run()
        except:
            tf.initialize_all_variables().run()

        self.g_sum = merge_summary([self.z_sum, self.d__sum, self.g_loss_eik_sum, self.g_loss_sym_sum,
                                    self.g_loss_gen_sum, self.g_sum, self.d_loss_fake_sum, self.g_loss_sum])
        self.d_sum = merge_summary(
            [self.z_sum, self.d_sum, self.d_loss_real_sum, self.d_loss_sum])

        self.writer = SummaryWriter(self.log_dir, self.sess.graph)
        sample_z = np.random.uniform(-1, 1, size=(self.sample_num, self.z_dim))
        sample_files = data[0:self.sample_num]
        sample = [np.load(sample_file)[0, :, :, :] for sample_file in sample_files]

        if (self.is_grayscale):
            sample_inputs = np.array(sample).astype(np.float32)
        else:
            sample_inputs = np.array(sample).astype(np.float32)

        counter = 1
        could_load, checkpoint_counter = self.load(self.checkpoint_dir)
        if could_load:
            counter = checkpoint_counter
            print(" [*] Load SUCCESS")
        else:
            print(" [!] Load failed...")

        d_accu_last_batch = .5
        batch_idxs = int(math.ceil(min(len(data), config.train_size) / self.glob_batch_size))
        total_steps = config.epoch * batch_idxs
        prev_time = -np.inf

        for epoch in xrange(config.epoch):
            # shuffle data before training in each epoch
            np.random.shuffle(data)
            for idx in xrange(0, batch_idxs):
                glob_batch_files = data[idx * self.glob_batch_size:(idx + 1) * self.glob_batch_size]
                glob_batch = [
                    np.load(batch_file)[0, :, :, :] for batch_file in glob_batch_files]
                glob_batch_images = np.array(glob_batch).astype(np.float32)[:, :, :, :, None]

                glob_batch_z = np.random.uniform(-1, 1, [self.glob_batch_size, self.z_dim]) \
                    .astype(np.float32)
                n_eff = len(glob_batch_files)

                # Pad zeros if effective batch size is smaller than global batch size
                if n_eff != self.glob_batch_size:
                    glob_batch_images = pad_glob_batch(glob_batch_images, self.glob_batch_size)

                # Update D network if accuracy in last batch <= 80%
                if d_accu_last_batch < .8:
                    # Update D network
                    _, summary_str = self.sess.run([d_optim, self.d_sum],
                                                   feed_dict={self.inputs: glob_batch_images,
                                                              self.z: glob_batch_z,
                                                              self.n_eff: n_eff})
                    self.writer.add_summary(summary_str, counter)

                # Update G network
                _, summary_str = self.sess.run([g_optim, self.g_sum],
                                               feed_dict={self.z: glob_batch_z,
                                                          self.n_eff: n_eff})

                # Compute last batch accuracy and losses
                d_accu_last_batch, errD_fake, errD_real, errG \
                    = self.sess.run([self.d_accu, self.d_loss_fake, self.d_loss_real, self.g_loss],
                                    feed_dict={self.inputs: glob_batch_images,
                                               self.z: glob_batch_z,
                                               self.n_eff: n_eff})
                self.writer.add_summary(summary_str, counter)

                # get time
                now_time = time.time()
                time_per_iter = now_time - prev_time
                prev_time = now_time
                eta = (total_steps - counter) * time_per_iter
                counter += 1

                print("Epoch: [%2d] [%4d/%4d] time/iter: %4.4f, eta(s): %4.4f, d_loss: %.8f, g_loss: %.8f, d_accu: %.4f"
                      % (epoch, idx, batch_idxs,
                         time_per_iter, eta, errD_fake + errD_real, errG, d_accu_last_batch))

                if np.mod(counter, 200) == 1:
                    try:
                        samples, d_loss, g_loss = self.sess.run(
                            [self.sampler, self.d_loss, self.g_loss],
                            feed_dict={
                                self.z: sample_z,
                                self.inputs: sample_inputs,
                            },
                        )
                        np.save(self.sample_dir+'/train_{:02d}_{:04d}.npy'
                                .format(config.sample_dir, epoch, idx), samples)
                        print("[Sample] d_loss: %.8f, g_loss: %.8f, d_accu: %.4f"
                              % (d_loss, g_loss, d_accu_last_batch))
                    except:
                        print("Error when saving samples.")

                if np.mod(counter, 200) == 2:
                    self.save(config.checkpoint_dir, counter)

        # save last checkpoint
        self.save(config.checkpoint_dir, counter)

    def discriminator(self, image, reuse=False):
        with tf.variable_scope("discriminator") as scope:
            if reuse:
                scope.reuse_variables()

            h0 = lrelu(conv3d(image, self.df_dim, name='d_h0_conv'))
            h1 = lrelu(self.d_bn1(conv3d(h0, self.df_dim * 2, name='d_h1_conv')))
            h2 = lrelu(self.d_bn2(conv3d(h1, self.df_dim * 4, name='d_h2_conv')))
            h3 = lrelu(self.d_bn3(conv3d(h2, self.df_dim * 8, name='d_h3_conv')))
            h4 = linear(tf.reshape(h3, [self.batch_size, -1]), 1, name='d_h3_lin')

            return tf.nn.sigmoid(h4), h4

    def generator(self, z):
        with tf.variable_scope("generator") as scope:
            s_d, s_h, s_w = self.output_depth, self.output_height, self.output_width
            s_d2, s_h2, s_w2 = conv_out_size_same(s_d, 2), conv_out_size_same(s_h, 2), conv_out_size_same(s_w, 2)
            s_d4, s_h4, s_w4 = conv_out_size_same(s_d2, 2), conv_out_size_same(s_h2, 2), conv_out_size_same(s_w2, 2)
            s_d8, s_h8, s_w8 = conv_out_size_same(s_d4, 2), conv_out_size_same(s_h4, 2), conv_out_size_same(s_w4, 2)
            s_d16, s_h16, s_w16 = conv_out_size_same(s_d8, 2), conv_out_size_same(s_h8, 2), conv_out_size_same(s_w8, 2)

            # project `z` and reshape
            self.z_, self.h0_w, self.h0_b = linear(
                z, self.gf_dim * 8 * s_d16 * s_h16 * s_w16, 'g_h0_lin', with_w=True)

            self.h0 = tf.reshape(
                self.z_, [-1, s_d16, s_h16, s_w16, self.gf_dim * 8])
            h0 = tf.nn.relu(self.g_bn0(self.h0))

            self.h1, self.h1_w, self.h1_b = deconv3d(
                h0, [self.batch_size, s_d8, s_h8, s_w8, self.gf_dim * 4], name='g_h1', with_w=True)
            h1 = tf.nn.relu(self.g_bn1(self.h1))

            h2, self.h2_w, self.h2_b = deconv3d(
                h1, [self.batch_size, s_d4, s_h4, s_w4, self.gf_dim * 2], name='g_h2', with_w=True)
            h2 = tf.nn.relu(self.g_bn2(h2))

            h3, self.h3_w, self.h3_b = deconv3d(
                h2, [self.batch_size, s_d2, s_h2, s_w2, self.gf_dim * 1], name='g_h3', with_w=True)
            h3 = tf.nn.relu(self.g_bn3(h3))

            h4, self.h4_w, self.h4_b = deconv3d(
                h3, [self.batch_size, s_d, s_h, s_w, self.c_dim], name='g_h4', with_w=True)

            h5 = low_pass(tf.nn.tanh(h4))
#             return tf.nn.tanh(h4)

            return h5


    def sampler(self, z):
        with tf.variable_scope("generator") as scope:
            scope.reuse_variables()

            s_d, s_h, s_w = self.output_depth, self.output_height, self.output_width
            s_d2, s_h2, s_w2 = conv_out_size_same(s_d, 2), conv_out_size_same(s_h, 2), conv_out_size_same(s_w, 2)
            s_d4, s_h4, s_w4 = conv_out_size_same(s_d2, 2), conv_out_size_same(s_h2, 2), conv_out_size_same(s_w2, 2)
            s_d8, s_h8, s_w8 = conv_out_size_same(s_d4, 2), conv_out_size_same(s_h4, 2), conv_out_size_same(s_w4, 2)
            s_d16, s_h16, s_w16 = conv_out_size_same(s_d8, 2), conv_out_size_same(s_h8, 2), conv_out_size_same(s_w8, 2)

            # project `z` and reshape
            h0 = tf.reshape(
                linear(z, self.gf_dim * 8 * s_d16 * s_h16 * s_w16, 'g_h0_lin'),
                [-1, s_d16, s_h16, s_w16, self.gf_dim * 8])
            h0 = tf.nn.relu(self.g_bn0(h0, train=False))

            h1 = deconv3d(h0, [self.batch_size, s_d8, s_h8, s_w8, self.gf_dim * 4], name='g_h1')
            h1 = tf.nn.relu(self.g_bn1(h1, train=False))

            h2 = deconv3d(h1, [self.batch_size, s_d4, s_h4, s_w4, self.gf_dim * 2], name='g_h2')
            h2 = tf.nn.relu(self.g_bn2(h2, train=False))

            h3 = deconv3d(h2, [self.batch_size, s_d2, s_h2, s_w2, self.gf_dim * 1], name='g_h3')
            h3 = tf.nn.relu(self.g_bn3(h3, train=False))

            h4 = deconv3d(h3, [self.batch_size, s_d, s_h, s_w, self.c_dim], name='g_h4')

            return tf.nn.tanh(h4)

    @property
    def model_dir(self):
        return "{}_{}_{}_{}_{}".format(
            self.dataset_name, self.batch_size,
            self.output_depth, self.output_height, self.output_width)

    def save(self, checkpoint_dir, step):
        model_name = "SDFGAN.model"
        checkpoint_dir = os.path.join(checkpoint_dir, self.model_dir)

        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)

        self.saver.save(self.sess,
                        os.path.join(checkpoint_dir, model_name),
                        global_step=step)

    def load(self, checkpoint_dir):
        import re
        print(" [*] Reading checkpoints...")
        checkpoint_dir = os.path.join(checkpoint_dir, self.model_dir)

        ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
            self.saver.restore(self.sess, os.path.join(checkpoint_dir, ckpt_name))
            counter = int(next(re.finditer("(\d+)(?!.*\d)", ckpt_name)).group(0))
            print(" [*] Success to read {}".format(ckpt_name))
            return True, counter
        else:
            print(" [*] Failed to find a checkpoint")
            return False, 0
