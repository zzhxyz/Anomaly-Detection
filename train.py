import argparse
import json
import pandas as pd
import csv
import datetime
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import requests
import numpy as np
from keras.preprocessing.image import ImageDataGenerator
from modules import utils as utils
from modules.utils import printProgressBar as printProgressBar
from modules.resmaps import calculate_resmaps
import modules.models.resnet as resnet
import modules.models.mvtec_2 as mvtec_2
import modules.models.mvtec as mvtec
from modules import metrics as custom_metrics
from modules import loss_functions as loss_functions
import keras.backend as K
from tensorflow import keras
import tensorflow as tf
import sys
import os
import time

from LR_Scheduler.lr_finder import LearningRateFinder
from LR_Scheduler.clr_callback import CyclicLR
from LR_Scheduler import config

import ktrain

"""
Created on Tue Dec 10 19:46:17 2019

@author: adnene33


Valid input arguments for color_mode and loss:

                        +----------------+----------------+
                        |       Model Architecture        |  
                        +----------------+----------------+
                        | mvtec, mvtec2  | Resnet, Nasnet |
========================+================+================+
        ||              |                |                |
        ||   grayscale  | SSIM, L2, MSE  |   Not Valid    |
Color   ||              |                |                |
Mode    ----------------+----------------+----------------+
        ||              |                |                |
        ||      RGB     | MSSIM, L2, MSE | MSSIM, L2, MSE |
        ||              |                |                |
--------+---------------+----------------+----------------+
"""


def main(args):
    # ========================= SETUP ==============================
    # Get training data setup
    directory = args.directory
    train_data_dir = os.path.join(directory, "train")
    nb_training_images_aug = args.nb_images
    batch_size = args.batch
    color_mode = args.color
    loss = args.loss.upper()
    validation_split = 0.1
    architecture = args.architecture
    tag = args.tag

    # check input arguments
    if architecture == "resnet" and color_mode == "grayscale":
        raise ValueError("ResNet expects rgb images")
    if architecture == "nasnet" and color_mode == "grayscale":
        raise ValueError("NasNet expects rgb images")
    if loss == "MSSIM" and color_mode == "grayscale":
        raise ValueError("MSSIM works only with rgb images")
    if loss == "SSIM" and color_mode == "rgb":
        raise ValueError("SSIM works only with grayscale images")

    # set chennels and metrics to monitor training
    if color_mode == "grayscale":
        channels = 1
        resmaps_mode = "SSIM"
        metrics = [custom_metrics.ssim_metric]
    elif color_mode == "rgb":
        channels = 3
        resmaps_mode = "MSSIM"
        metrics = [custom_metrics.mssim_metric]

    # build model
    if architecture == "mvtec":
        model = mvtec.build_model(channels)
    elif architecture == "mvtec2":
        model = mvtec_2.build_model(channels)
    elif architecture == "resnet":
        model, base_encoder = resnet.build_model()
    elif architecture == "nasnet":
        raise Exception("Nasnet ist not yet implemented.")
        # model, base_encoder = models.build_nasnet()
        # sys.exit()

    # set loss function
    if loss == "SSIM":
        loss_function = loss_functions.ssim_loss
    elif loss == "MSSIM":
        loss_function = loss_functions.mssim_loss
    elif loss == "L2":
        loss_function = loss_functions.l2_loss
    elif loss == "MSE":
        loss_function = "mean_squared_error"

    # specify model name and directory to save model
    now = datetime.datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
    save_dir = os.path.join(
        os.getcwd(), "saved_models", directory, architecture, loss, now
    )
    if not os.path.isdir(save_dir):
        os.makedirs(save_dir)
    model_name = "CAE_" + architecture + "_b{}".format(batch_size)
    model_path = os.path.join(save_dir, model_name + ".h5")

    # specify logging directory for tensorboard visualization
    log_dir = os.path.join(save_dir, "logs")
    if not os.path.isdir(log_dir):
        os.makedirs(log_dir)

    # set callbacks
    early_stopping_cb = keras.callbacks.EarlyStopping(
        monitor="val_loss", patience=12, mode="min", verbose=1,
    )
    checkpoint_cb = keras.callbacks.ModelCheckpoint(
        filepath=model_path,
        monitor="val_loss",
        verbose=1,
        save_best_only=False,  # True
        save_weights_only=False,
        period=1,
    )
    tensorboard_cb = keras.callbacks.TensorBoard(
        log_dir=log_dir, write_graph=True, update_freq="epoch"
    )

    # ============================= PREPROCESSING ===============================

    if architecture in ["mvtec", "mvtec2"]:
        rescale = 1.0 / 255
        shape = (256, 256)
        preprocessing_function = None
        preprocessing = None
    elif architecture == "resnet":
        rescale = None
        shape = (299, 299)
        preprocessing_function = keras.applications.inception_resnet_v2.preprocess_input
        preprocessing = "keras.applications.inception_resnet_v2.preprocess_input"
    elif architecture == "nasnet":
        rescale = None
        shape = (224, 224)
        preprocessing_function = keras.applications.nasnet.preprocess_input
        preprocessing = "keras.applications.inception_resnet_v2.preprocess_input"
        pass

    print("[INFO] Using Keras's flow_from_directory method...")
    # This will do preprocessing and realtime data augmentation:
    train_datagen = ImageDataGenerator(
        # randomly rotate images in the range (degrees, 0 to 180)
        rotation_range=5,
        # randomly shift images horizontally (fraction of total width)
        width_shift_range=0.05,
        # randomly shift images vertically (fraction of total height)
        height_shift_range=0.05,
        # set mode for filling points outside the input boundaries
        fill_mode="nearest",
        # value used for fill_mode = "constant"
        cval=0.0,
        # randomly change brightness (darker < 1 < brighter)
        brightness_range=[0.95, 1.05],
        # set rescaling factor (applied before any other transformation)
        rescale=rescale,
        # set function that will be applied on each input
        preprocessing_function=preprocessing_function,
        # image data format, either "channels_first" or "channels_last"
        data_format="channels_last",
        # fraction of images reserved for validation (strictly between 0 and 1)
        validation_split=validation_split,
    )

    # For validation dataset, only rescaling
    validation_datagen = ImageDataGenerator(
        rescale=rescale,
        data_format="channels_last",
        validation_split=validation_split,
        preprocessing_function=preprocessing_function,
    )

    # Generate training batches with datagen.flow_from_directory()
    train_generator = train_datagen.flow_from_directory(
        directory=train_data_dir,
        target_size=shape,
        color_mode=color_mode,
        batch_size=batch_size,
        class_mode="input",
        subset="training",
        shuffle=True,
    )

    # Generate validation batches with datagen.flow_from_directory()
    validation_generator = validation_datagen.flow_from_directory(
        directory=train_data_dir,
        target_size=shape,
        color_mode=color_mode,
        batch_size=batch_size,
        class_mode="input",
        subset="validation",
        shuffle=True,
    )

    # Print command to paste in browser for visualizing in Tensorboard
    print("\ntensorboard --logdir={}\n".format(log_dir))

    # calculate epochs
    epochs = nb_training_images_aug // train_generator.samples

    # =============================== TRAINING =================================

    # define configuration for LR_find
    print("[INFO] initializing LR_find configuration...")
    if loss == "SSIM":
        max_epochs = 10
        stop_factor = -6
    elif loss == "L2":
        max_epochs = None
        stop_factor = 6
    start_lr = 1e-7

    # initialize the optimizer and compile model
    print("[INFO] compiling model...")
    optimizer = keras.optimizers.Adam(learning_rate=start_lr)
    model.compile(
        loss=loss_function, optimizer=optimizer, metrics=metrics,
    )

    # wrap model and data in ktrain.Learner object
    learner = ktrain.get_learner(
        model=model,
        train_data=train_generator,
        val_data=validation_generator,
        # workers=8,
        use_multiprocessing=False,
        batch_size=batch_size,
    )

    # if args.lr_find > 0:
    # find good learning rate
    learner.lr_find(
        start_lr=start_lr,
        lr_mult=1.01,
        max_epochs=max_epochs,
        stop_factor=stop_factor,
        show_plot=False,
        verbose=1,
    )
    learner.lr_plot()
    plt.savefig(os.path.join(save_dir, "lr_find_plot.png"))
    plt.show(block=True)
    print("[INFO] learning rate finder complete")
    print("[INFO] examine plot and adjust learning rates before training")

    # prompt user to enter max learning rate
    max_lr = float(input("Enter max learning rate: "))

    # start training
    history = learner.fit_onecycle(
        lr=max_lr,
        epochs=epochs,
        cycle_momentum=True,
        max_momentum=0.95,
        min_momentum=0.85,
        verbose=1,
    )

    # Save model
    tf.keras.models.save_model(
        model, model_path, include_optimizer=True, save_format="h5"
    )
    print("Saved trained model at %s " % model_path)

    # save loss plot
    plt.figure()
    learner.plot(plot_type="loss")
    plt.savefig(os.path.join(save_dir, "loss_plot.png"))
    print("loss plot saved at {} ".format(save_dir))

    # save lr plot
    plt.figure()
    learner.plot(plot_type="lr")
    plt.savefig(os.path.join(save_dir, "lr_plot.png"))
    print("learning rate plot saved at {} ".format(save_dir))

    # save training setup and model configuration
    setup = {
        "data_setup": {
            "directory": directory,
            "nb_training_images": train_generator.samples,
            "nb_validation_images": validation_generator.samples,
        },
        "preprocessing_setup": {
            "rescale": rescale,
            "shape": shape,
            "preprocessing": preprocessing,
        },
        "lr_finder": {
            "start_lr": start_lr,
            "max_lr": max_lr,
            "stop_factor": stop_factor,
            "max_epochs": max_epochs,
        },
        "train_setup": {
            "architecture": architecture,
            "nb_training_images_aug": nb_training_images_aug,
            "epochs": epochs,
            "max_lr": max_lr,
            "min_lr": max_lr / 10,
            "batch_size": batch_size,
            "loss": loss,
            "color_mode": color_mode,
            "channels": channels,
            "validation_split": validation_split,
        },
        "tag": tag,
    }

    with open(os.path.join(save_dir, "setup.json"), "w") as json_file:
        json.dump(setup, json_file, indent=4, sort_keys=False)

    if args.inspect == True:
        # INSPECTING VALIDATION IMAGES
        print("[INFO] inspecting validation images...")

        # create a directory to save inspection plots
        inspection_val_dir = os.path.join(save_dir, "inspection_val")
        if not os.path.isdir(inspection_val_dir):
            os.makedirs(inspection_val_dir)

        # create a generator that yields preprocessed validation images
        inspection_val_generator = validation_datagen.flow_from_directory(
            directory=train_data_dir,
            target_size=shape,
            color_mode=color_mode,
            batch_size=validation_generator.samples,
            class_mode="input",
            subset="validation",
            shuffle=False,
        )
        imgs_val_input = inspection_val_generator.next()[0]
        filenames = inspection_val_generator.filenames

        # predict on validation images
        print("[INFO] reconstructing validation images...")
        imgs_val_pred = model.predict(imgs_val_input)

        # save input and pred arrays
        print(
            "[INFO] saving input and pred validation images at {}...".format(
                inspection_val_dir
            )
        )
        utils.save_np(imgs_val_input, inspection_val_dir, "imgs_val_input.npy")
        utils.save_np(imgs_val_pred, inspection_val_dir, "imgs_val_input.npy")

        # compute resmaps by substracting pred out of input
        resmaps_val_diff = imgs_val_input - imgs_val_pred

        # compute resmaps using the ssim method
        resmaps_val_ssim = calculate_resmaps(
            imgs_val_input, imgs_val_pred, method="SSIM"
        )

        # compute resmaps using the L2 method
        resmaps_val_l2 = calculate_resmaps(imgs_val_input, imgs_val_pred, method="L2")

        # # convert to grayscale if necessary
        # if color_mode == "rgb":
        #     resmaps_val_ssim = tf.image.rgb_to_grayscale(resmaps_val)

        # generate and save inspection images
        print("[INFO] generating inspection plots on validation images...")
        l = len(filenames)
        printProgressBar(0, l, prefix="Progress:", suffix="Complete", length=50)
        for i in range(len(imgs_val_input)):
            f, axarr = plt.subplots(3, 2)
            f.set_size_inches((8, 9))
            axarr[0, 0].imshow(imgs_val_input[i, :, :, 0], cmap="gray")
            axarr[0, 0].set_title("input")
            axarr[0, 0].set_axis_off()
            axarr[0, 1].imshow(imgs_val_pred[i, :, :, 0], cmap="gray")
            axarr[0, 1].set_title("pred")
            axarr[0, 1].set_axis_off()
            axarr[1, 0].imshow(resmaps_val_diff[i, :, :, 0], cmap="gray")
            axarr[1, 0].set_title("resmap_diff")
            axarr[1, 0].set_axis_off()
            axarr[1, 1].imshow(resmaps_val_ssim[i, :, :, 0], cmap="gray")
            axarr[1, 1].set_title("resmap_ssim")
            axarr[1, 1].set_axis_off()
            axarr[2, 0].imshow(resmaps_val_l2[i, :, :, 0], cmap="gray")
            axarr[2, 0].set_title("resmap_L2")
            axarr[2, 0].set_axis_off()
            axarr[2, 1].set_axis_off()
            plt.suptitle("VALIDATION\n" + filenames[i])
            plot_name = utils.get_plot_name(filenames[i], suffix="inspection")
            f.savefig(os.path.join(inspection_val_dir, plot_name))
            plt.close(fig=f)
            # print progress bar
            time.sleep(0.1)
            printProgressBar(i + 1, l, prefix="Progress:", suffix="Complete", length=50)

        # INSPECTING TEST IMAGES
        print("[INFO] inspecting test images...")

        # create a directory to save inspection plots
        inspection_test_dir = os.path.join(save_dir, "inspection_test")
        if not os.path.isdir(inspection_test_dir):
            os.makedirs(inspection_test_dir)

        test_datagen = ImageDataGenerator(
            rescale=rescale,
            data_format="channels_last",
            preprocessing_function=preprocessing_function,
        )
        test_data_dir = os.path.join(directory, "test")
        total_number = utils.get_total_number_test_images(test_data_dir)

        # retrieve preprocessed test images as a numpy array
        inspection_test_generator = test_datagen.flow_from_directory(
            directory=test_data_dir,
            target_size=shape,
            color_mode=color_mode,
            batch_size=total_number,
            shuffle=False,
            class_mode="input",
        )
        imgs_test_input = inspection_test_generator.next()[0]
        filenames = inspection_test_generator.filenames

        # predict on test images
        print("[INFO] reconstructing test images...")
        imgs_test_pred = model.predict(imgs_test_input)

        # save input and pred arrays
        print(
            "[INFO] saving input and pred test images at {}...".format(
                inspection_test_dir
            )
        )
        utils.save_np(imgs_test_input, inspection_test_dir, "imgs_test_input.npy")
        utils.save_np(imgs_test_pred, inspection_test_dir, "imgs_test_input.npy")

        # compute resmaps by substracting pred out of input
        resmaps_test_diff = imgs_test_input - imgs_test_pred

        # # convert to grayscale if necessary
        # if color_mode == "rgb":
        #     resmaps_test_ssim = tf.image.rgb_to_grayscale(resmaps_test)

        # compute resmaps using the ssim method
        resmaps_test_ssim = calculate_resmaps(
            imgs_test_input, imgs_test_pred, method="SSIM"
        )

        # compute resmaps using the L2 method
        resmaps_test_l2 = calculate_resmaps(
            imgs_test_input, imgs_test_pred, method="L2"
        )

        # generate and save inspection images
        print("[INFO] generating inspection plots on test images...")
        l = len(filenames)
        printProgressBar(0, l, prefix="Progress:", suffix="Complete", length=50)
        for i in range(len(imgs_test_input)):
            f, axarr = plt.subplots(3, 2)
            f.set_size_inches((8, 9))
            axarr[0, 0].imshow(imgs_test_input[i, :, :, 0], cmap="gray")
            axarr[0, 0].set_title("input")
            axarr[0, 0].set_axis_off()
            axarr[0, 1].imshow(imgs_test_pred[i, :, :, 0], cmap="gray")
            axarr[0, 1].set_title("pred")
            axarr[0, 1].set_axis_off()
            axarr[1, 0].imshow(resmaps_test_diff[i, :, :, 0], cmap="gray")
            axarr[1, 0].set_title("resmap_diff")
            axarr[1, 0].set_axis_off()
            axarr[1, 1].imshow(resmaps_test_ssim[i, :, :, 0], cmap="gray")
            axarr[1, 1].set_title("resmap_ssim")
            axarr[1, 1].set_axis_off()
            axarr[2, 0].imshow(resmaps_test_l2[i, :, :, 0], cmap="gray")
            axarr[2, 0].set_title("resmap_L2")
            axarr[2, 0].set_axis_off()
            axarr[2, 1].set_axis_off()
            plt.suptitle("TEST\n" + filenames[i])
            plot_name = utils.get_plot_name(filenames[i], suffix="inspection")
            f.savefig(os.path.join(inspection_test_dir, plot_name))
            plt.close(fig=f)
            # print progress bar
            time.sleep(0.1)
            printProgressBar(i + 1, l, prefix="Progress:", suffix="Complete", length=50)

        print("[INFO] done.")
        print("[INFO] all generated files are saved at: \n{}".format(save_dir))
        print("exiting script...")


if __name__ == "__main__":
    # create parser
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-d",
        "--directory",
        type=str,
        required=True,
        metavar="",
        help="training directory",
    )

    parser.add_argument(
        "-a",
        "--architecture",
        type=str,
        required=True,
        metavar="",
        choices=["mvtec", "mvtec2", "resnet", "nasnet"],
        help="model to use in training",
    )

    parser.add_argument(
        "-n",
        "--nb-images",
        type=int,
        default=10000,
        metavar="",
        help="number of training images",
    )
    parser.add_argument(
        "-b", "--batch", type=int, required=True, metavar="", help="batch size"
    )
    parser.add_argument(
        "-l",
        "--loss",
        type=str,
        required=True,
        metavar="",
        choices=["mssim", "ssim", "l2", "mse"],
        help="loss function used during training",
    )

    parser.add_argument(
        "-c",
        "--color",
        type=str,
        required=True,
        metavar="",
        choices=["rgb", "grayscale"],
        help="color mode",
    )

    # parser.add_argument(
    #     "-f",
    #     "--lr-find",
    #     type=int,
    #     default=0,
    #     help="whether or not to find optimal learning rate",
    # )

    parser.add_argument(
        "-i",
        "--inspect",
        type=bool,
        default=True,
        help="whether or not to find optimal learning rate",
    )

    parser.add_argument(
        "-t", "--tag", type=str, help="give a tag to the model to be trained"
    )

    args = parser.parse_args()
    if tf.test.is_gpu_available():
        print("[INFO] GPU was detected...")
    else:
        print("[INFO] No GPU was detected. CNNs can be very slow without a GPU...")
    print("[INFO] Tensorflow version: {} ...".format(tf.__version__))
    print("[INFO] Keras version: {} ...".format(keras.__version__))
    main(args)

# Examples of commands to initiate training

# python3 train.py -d mvtec/capsule -a mvtec2 -b 8 -l ssim -c grayscale --inspect True

# python3 train.py -d werkstueck/data_a30_nikon_weiss_edit -a mvtec2 -b 12 -l l2 -c grayscale --inspect True

# RESNET not yet supported
# python3 train.py -d mvtec/capsule -a resnet -b 12 -l mssim -c rgb


# elif architecture in ["resnet", "nasnet"]:

#     # Phase 1: train the decoder with frozen encoder
#     epochs_1 = int(np.ceil(0.7 * epochs))

#     for layer in base_encoder.layers:
#         layer.trainable = False

#     # print(base_encoder.summary())
#     print(model.summary())

#     learning_rate_1 = 2e-4
#     decay_1 = 1e-5

#     optimizer = keras.optimizers.Adam(
#         learning_rate=learning_rate_1, beta_1=0.9, beta_2=0.999, decay=decay_1
#     )

#     model.compile(
#         loss=loss_function, optimizer=optimizer, metrics=metrics,
#     )

#     # Fit the model on the batches generated by datagen.flow_from_directory()
#     history_1 = model.fit_generator(
#         generator=train_generator,
#         epochs=epochs_1,  #
#         steps_per_epoch=train_generator.samples // batch_size,
#         validation_data=validation_generator,
#         validation_steps=validation_generator.samples // batch_size,
#         # callbacks=[checkpoint_cb],
#     )

#     # Phase 2: train both encoder and decoder together
#     epochs_2 = epochs - epochs_1

#     for layer in base_encoder.layers:
#         layer.trainable = True

#     # print(base_encoder.summary())
#     print(model.summary())

#     # learning_rate_2 = 1e-5
#     # decay_2 = 1e-6

#     # optimizer = keras.optimizers.Adam(
#     #     learning_rate=learning_rate_2, beta_1=0.9, beta_2=0.999, decay=decay_2
#     # )

#     model.compile(
#         loss=loss_function, optimizer=optimizer, metrics=metrics,
#     )

#     # train for the remaining epochs
#     history_2 = model.fit_generator(
#         generator=train_generator,
#         epochs=epochs_2,  #
#         steps_per_epoch=train_generator.samples // batch_size,
#         validation_data=validation_generator,
#         validation_steps=validation_generator.samples // batch_size,
#         # callbacks=[checkpoint_cb],
#     )

#     # wrap training hyper-parameters of both phases
#     epochs = [epochs_1, epochs_2]
#     learning_rate = [learning_rate_1, learning_rate_2]
#     decay = [decay_1, decay_2]
#     history = utils.extend_dict(history_1.history, history_2.history)
