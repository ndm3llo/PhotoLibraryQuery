import io

import imagehash
import torch  # noqa: F401
from PIL import Image
from sentence_transformers import SentenceTransformer


class AIService:
    def __init__(self):
        # Load CLIP model
        self.model = SentenceTransformer('clip-ViT-B-32')

    def generate_embedding_from_bytes(self, image_bytes: bytes):
        """Converts raw upload bytes into a 512-dimension vector."""
        # Convert bytes to PIL and ensure it's RGB (removes transparency/alpha channel)
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        embedding = self.model.encode(img)
        return embedding.tolist()
    
    def generate_embedding(self, image_path: str):
        # Open image and convert to vector
        img = Image.open(image_path)
        embedding = self.model.encode(img)
        return embedding.tolist()

    def generate_text_embedding(self, text: str):
        # Converts a string into a 512-dimension vector
        embedding = self.model.encode(text)
        return embedding.tolist()
    
    def generate_phash(self, image_bytes: bytes):
        # We use io.BytesIO so we can hash the image without saving it to disk first
        img = Image.open(io.BytesIO(image_bytes))
        return str(imagehash.phash(img))

ai_service = AIService()