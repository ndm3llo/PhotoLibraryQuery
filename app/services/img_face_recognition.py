import io

import numpy as np
from deepface import DeepFace
from PIL import Image


def extract_face_embeddings(image_bytes: bytes):
    """
    Detects faces and returns a list of 512-d vectors.
    Cost: $0 (Runs on your CPU/GPU)
    """
    try:
        # Convert bytes to a format DeepFace likes
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_array = np.array(img)

        # 'Facenet512' is highly accurate and matches your 512-d schema
        results = DeepFace.represent(
            img_path=img_array, 
            model_name="Facenet512", 
            enforce_detection=False, # Don't crash if no face is found
            detector_backend="retinaface" # Accurate but free
        )

        return [res["embedding"] for res in results if res.get("face_confidence", 0) > 0.6]
    except Exception as e:
        print(f"Face extraction error: {e}")
        return []