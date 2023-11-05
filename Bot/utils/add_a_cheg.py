import cv2
import numpy as np
import os


class ChegClass:
    @staticmethod
    def add_my_cheg(root_path=None):
        if root_path is None:
            root_path = ""

        background = cv2.imread(root_path + "tmp/fig_to_send.jpg", cv2.IMREAD_UNCHANGED)
        foreground = cv2.imread(
            root_path + "utils/cheggers-removebg-preview.png", cv2.IMREAD_UNCHANGED
        )
        factor = np.random.uniform(5, 9)
        # factor = 10
        foreground = cv2.resize(foreground, (0, 0), fx=factor, fy=factor)

        def rotate_image(image, angle):
            image_center = tuple(np.array(image.shape[1::-1]) / 2)
            rot_mat = cv2.getRotationMatrix2D(image_center, angle, 1.0)
            result = cv2.warpAffine(
                image, rot_mat, image.shape[1::-1], flags=cv2.INTER_LINEAR
            )
            return result

        foreground = rotate_image(foreground, np.random.randint(0, 360))

        # foreground = cv2.resize(foreground, (100, 200))
        def overlay_transparent(background, overlay, x, y):
            background_width = background.shape[1]
            background_height = background.shape[0]

            if x >= background_width or y >= background_height:
                return background

            h, w = overlay.shape[0], overlay.shape[1]

            if x + w > background_width:
                w = background_width - x
                overlay = overlay[:, :w]

            if y + h > background_height:
                h = background_height - y
                overlay = overlay[:h]

            if overlay.shape[2] < 4:
                overlay = np.concatenate(
                    [
                        overlay,
                        np.ones(
                            (overlay.shape[0], overlay.shape[1], 1), dtype=overlay.dtype
                        )
                        * 255,
                    ],
                    axis=2,
                )

            overlay_image = overlay[..., :3]
            mask = overlay[..., 3:] / 255.0
            a_channel = np.ones(mask.shape, dtype=float) / np.random.uniform(1, 10)
            mask = mask * a_channel

            background[y : y + h, x : x + w] = (1.0 - mask) * background[
                y : y + h, x : x + w
            ] + mask * overlay_image

            return background

        img = overlay_transparent(
            background,
            foreground,
            np.random.randint(0, background.shape[0] - foreground.shape[0]),
            y=np.random.randint(0, background.shape[1] - foreground.shape[1]),
        )

        cv2.imwrite(root_path + "tmp/to_send_cheg.jpg", img)
