# Models

`face_detection_yunet_2023mar.onnx` — YuNet face detector from OpenCV Zoo
(https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet), MIT licence.
Used by `tools/img_for_web.py` to frame headshots on the face. 228 KB; runs on CPU via OpenCV's
`cv2.FaceDetectorYN` (opencv-python-headless >= 4.8).
