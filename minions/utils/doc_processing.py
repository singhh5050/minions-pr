import os
import tempfile
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Union, Tuple, Any
from urllib.parse import urlparse
from pathlib import Path
import sys
import os
import requests
from PIL import Image
from pdf2image import convert_from_path, convert_from_bytes
from docling_core.types.doc import ImageRefMode
from docling_core.types.doc.document import DocTagsDocument, DoclingDocument

# Try to import MLX VLM, but make it optional
try:
    import mlx_vlm
    from mlx_vlm import load, generate
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config, stream_generate

    HAS_MLX = True
except ImportError:
    HAS_MLX = False

# Try to import transformers for the non-MLX path
try:
    import torch
    from transformers import AutoProcessor, AutoModelForVision2Seq

    HAS_TRANSFORMERS = True
except ImportError:
    HAS_TRANSFORMERS = False


def pdf_to_images(
    pdf_data: Union[str, Path, bytes], dpi: int = 300
) -> List[Image.Image]:
    """
    Convert a PDF file to a list of PIL Image objects.

    Args:
        pdf_data: Path to the PDF file or PDF data as bytes
        dpi: Resolution for the conversion (higher means better quality but larger images)

    Returns:
        List of PIL Image objects, one per page
    """
    # Windows-specific file handling
    pdf_path = None
    if isinstance(pdf_data, (str, Path)) and sys.platform.startswith('win'):
        # Windows path normalization and validation
        if isinstance(pdf_data, (str, Path)) and sys.platform.startswith('win'):
            pdf_path = Path(pdf_data).expanduser().resolve().as_posix().replace('/', '\\')
            
            # Unified exception handling
            try:
                with open(pdf_path, 'a'):
                    if not os.path.getsize(pdf_path):
                        raise ValueError(f"Empty PDF file: {pdf_path}")
            except (PermissionError, FileNotFoundError) as e:
                raise type(e)(f"PDF access failed: {pdf_path}") from e
    
        try:
            # Unified input type handling
            input_source = pdf_path if sys.platform.startswith('win') else pdf_data
            print(f"Processing PDF: {getattr(input_source, 'name', input_source)}")
            
            return (convert_from_path(input_source, dpi=dpi) if isinstance(input_source, (str, Path)) 
                    else convert_from_bytes(pdf_data, dpi=dpi))
    
        except Exception as e:
            # Enhance error diagnostics
            error_msg = f"PDF processing failed: {str(e)}"
            if sys.platform.startswith('win'):
                error_msg += "\nWindows-specific troubleshooting:\n"
                error_msg += f"- Path encoding: {sys.getfilesystemencoding()}\n"
                error_msg += f"- Absolute path: {os.path.abspath(pdf_path) if pdf_path else 'N/A'}\n"
                error_msg += "- Try using short path name: " + subprocess.getoutput(f'powershell "(Get-Item -LiteralPath '{pdf_path}').FullName"') if pdf_path else ""
                
            # Check underlying dependencies
            try:
                from pdf2image.exceptions import PDFInfoNotInstalledError
                if isinstance(e, PDFInfoNotInstalledError):
                    error_msg += "\nMissing dependency: Install poppler-utils (Windows) or poppler (Mac/Linux)"
            except ImportError:
                pass
                
            raise RuntimeError(error_msg) from e

def img_to_markdown_smoldocling(
    image_data: Union[str, Path, Image.Image, bytes],
    prompt: str = "Convert this page to docling.",
    model_path: str = "ds4sd/SmolDocling-256M-preview-mlx-bf16",
    verbose: bool = False,
    max_tokens: int = 4096,
    model_and_processor=None,
    config=None,
    use_mlx: bool = None,
) -> str:
    """
    Convert an image or PDF to markdown using SmolDocling.

    Args:
        image_data: Path to image file, URL, PIL Image object, image/PDF data as bytes,
                   or base64-encoded image string
        prompt: Prompt to guide the conversion
        model_path: Path to the SmolDocling model
        verbose: Whether to print progress
        max_tokens: Maximum number of tokens to generate
        model_and_processor: Optional tuple of (model, processor) to avoid reloading
        config: Optional model config to avoid reloading
        use_mlx: Whether to use MLX implementation. If None, will use MLX if available.

    Returns:
        Markdown representation of the document
    """
    # Determine whether to use MLX or transformers
    if use_mlx is None:
        use_mlx = HAS_MLX

    if use_mlx and not HAS_MLX:
        raise ImportError(
            "MLX VLM is not installed but was requested. Install with 'pip install mlx-vlm' OR use transformers implementation by setting use_mlx=False"
        )

    if not use_mlx and not HAS_TRANSFORMERS:
        raise ImportError(
            "Transformers is not installed but was requested. Install with 'pip install transformers torch' OR use MLX implementation by setting use_mlx=True"
        )

    # Handle different input types to get a PIL image
    pil_image = _process_image_input(image_data)

    # Generate markdown using either MLX or transformers
    if use_mlx:
        return _generate_markdown_with_mlx(
            pil_image,
            prompt,
            model_path,
            verbose,
            max_tokens,
            model_and_processor,
            config,
        )
    else:
        return _generate_markdown_with_transformers(
            pil_image, prompt, model_path, verbose, max_tokens, model_and_processor
        )


def _process_image_input(
    image_data: Union[str, Path, Image.Image, bytes],
) -> Image.Image:
    """Process various image input types and return a PIL Image."""
    if isinstance(image_data, (str, Path)):
        # Check if it's a base64 encoded string
        if (
            isinstance(image_data, str)
            and image_data.startswith(("data:image", "data:application/pdf"))
            or (len(image_data) > 100 and "," in image_data[:100])
        ):
            # Extract the base64 data after the comma if it's a data URL
            if "," in image_data:
                base64_data = image_data.split(",", 1)[1]
            else:
                base64_data = image_data

            import base64

            image_bytes = base64.b64decode(base64_data)

            try:
                # Try to open as an image
                pil_image = Image.open(BytesIO(image_bytes))
            except Exception:
                # If that fails, try to process as PDF
                try:
                    images = pdf_to_images(image_bytes)
                    if not images:
                        raise ValueError("Could not extract images from PDF bytes")
                    pil_image = images[0]  # Use first page
                except Exception as e:
                    raise ValueError(
                        f"Could not process base64 data as image or PDF: {e}"
                    )
        # Check if it's a PDF
        elif str(image_data).lower().endswith(".pdf"):
            # Convert first page of PDF to image
            images = pdf_to_images(image_data)
            if not images:
                raise ValueError(f"Could not extract images from PDF: {image_data}")
            pil_image = images[0]  # Use first page
        elif urlparse(str(image_data)).scheme != "":  # it's a URL
            response = requests.get(image_data, stream=True, timeout=10)
            response.raise_for_status()
            pil_image = Image.open(BytesIO(response.content))
        else:  # Local image file
            pil_image = Image.open(image_data)
    elif isinstance(image_data, Image.Image):
        pil_image = image_data
    elif isinstance(image_data, bytes):
        # Try to determine if it's a PDF or image
        try:
            # First try to open as an image
            pil_image = Image.open(BytesIO(image_data))
        except Exception:
            # If that fails, try to process as PDF
            try:
                images = pdf_to_images(image_data)
                if not images:
                    raise ValueError("Could not extract images from PDF bytes")
                pil_image = images[0]  # Use first page
            except Exception as e:
                raise ValueError(f"Could not process bytes as image or PDF: {e}")
    else:
        raise TypeError(
            "image_data must be a string path, Path object, PIL Image, bytes, or base64-encoded string"
        )

    return pil_image


def _generate_markdown_with_mlx(
    pil_image: Image.Image,
    prompt: str,
    model_path: str,
    verbose: bool,
    max_tokens: int,
    model_and_processor=None,
    config=None,
) -> str:
    """Generate markdown using MLX implementation."""
    if not HAS_MLX:
        raise ImportError(
            "MLX VLM is not installed. Install with 'pip install mlx-vlm'"
        )

    # Load the model if not provided
    if model_and_processor is None or config is None:
        model, processor = mlx_vlm.load(model_path)
        config = mlx_vlm.utils.load_config(model_path)
    else:
        model, processor = model_and_processor

    # Apply chat template
    formatted_prompt = apply_chat_template(processor, config, prompt, num_images=1)

    # Generate output
    output = ""
    for token in stream_generate(
        model,
        processor,
        formatted_prompt,
        [pil_image],
        max_tokens=max_tokens,
        verbose=verbose,
    ):
        output += token.text
        if verbose:
            print(token.text, end="")
        if "</doctag>" in token.text:
            break

    # Populate document
    doctags_doc = DocTagsDocument.from_doctags_and_image_pairs([output], [pil_image])
    doc = DoclingDocument(name="Document")
    doc.load_from_doctags(doctags_doc)

    # Export as markdown
    return doc.export_to_markdown()


def _generate_markdown_with_transformers(
    pil_image: Image.Image,
    prompt: str,
    model_path: str,
    verbose: bool,
    max_tokens: int,
    model_and_processor=None,
) -> str:
    """Generate markdown using Transformers implementation."""
    if not HAS_TRANSFORMERS:
        raise ImportError(
            "Transformers is not installed. Install with 'pip install transformers torch'"
        )

    # Adjust model path for transformers if needed
    if "mlx" in model_path:
        model_path = "ds4sd/SmolDocling-256M-preview"

    # Load the model if not provided
    if model_and_processor is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        processor = AutoProcessor.from_pretrained(model_path)
        model = AutoModelForVision2Seq.from_pretrained(
            model_path,
            torch_dtype=torch.bfloat16,
            _attn_implementation="flash_attention_2" if device == "cuda" else "eager",
        ).to(device)
    else:
        model, processor = model_and_processor
        device = model.device

    # Create input messages
    messages = [
        {
            "role": "user",
            "content": [{"type": "image"}, {"type": "text", "text": prompt}],
        },
    ]

    # Prepare inputs
    formatted_prompt = processor.apply_chat_template(
        messages, add_generation_prompt=True
    )
    inputs = processor(text=formatted_prompt, images=[pil_image], return_tensors="pt")
    inputs = inputs.to(device)

    # Generate outputs
    if verbose:
        print("Generating with transformers...")

    generated_ids = model.generate(**inputs, max_new_tokens=max_tokens)
    prompt_length = inputs.input_ids.shape[1]
    trimmed_generated_ids = generated_ids[:, prompt_length:]
    doctags = processor.batch_decode(
        trimmed_generated_ids,
        skip_special_tokens=False,
    )[0].lstrip()

    if verbose:
        print(doctags)

    # Populate document
    doctags_doc = DocTagsDocument.from_doctags_and_image_pairs([doctags], [pil_image])
    doc = DoclingDocument(name="Document")
    doc.load_from_doctags(doctags_doc)

    # Export as markdown
    return doc.export_to_markdown()


def process_pdf_to_markdown(
    pdf_data: Union[str, Path, bytes],
    prompt: str = "Convert this page to docling.",
    model_path: str = "ds4sd/SmolDocling-256M-preview-mlx-bf16",
    verbose: bool = False,
    max_tokens: int = 4096,
    return_type: str = "string",
    use_mlx: bool = None,
) -> Union[str, List[str]]:
    """
    Process all pages of a PDF and convert each to markdown.

    Args:
        pdf_data: Path to the PDF file or PDF data as bytes
        prompt: Prompt to guide the conversion
        model_path: Path to the SmolDocling model
        verbose: Whether to print progress
        max_tokens: Maximum number of tokens to generate
        return_type: Whether to return a concatenated string or a list of strings
        use_mlx: Whether to use MLX implementation. If None, will use MLX if available.
    Returns:
        List of markdown strings, one per page or a single concatenated string
    """
    images = pdf_to_images(pdf_data)
    markdown_pages = []

    # Determine whether to use MLX or transformers
    if use_mlx is None:
        use_mlx = HAS_MLX

    # Load model once for all pages
    if use_mlx and HAS_MLX:
        model, processor = mlx_vlm.load(model_path)
        config = mlx_vlm.utils.load_config(model_path)
        model_and_processor = (model, processor)
    elif not use_mlx and HAS_TRANSFORMERS:
        # Adjust model path for transformers if needed
        if "mlx" in model_path:
            transformers_model_path = "ds4sd/SmolDocling-256M-preview"
        else:
            transformers_model_path = model_path

        device = "cuda" if torch.cuda.is_available() else "cpu"
        processor = AutoProcessor.from_pretrained(transformers_model_path)
        model = AutoModelForVision2Seq.from_pretrained(
            transformers_model_path,
            torch_dtype=torch.bfloat16,
            _attn_implementation="flash_attention_2" if device == "cuda" else "eager",
        ).to(device)
        model_and_processor = (model, processor)
        config = None
    else:
        model_and_processor = None
        config = None

    for i, img in enumerate(images):
        if verbose:
            print(f"Processing page {i+1}/{len(images)}...")
        markdown = img_to_markdown_smoldocling(
            img,
            prompt,
            model_path,
            verbose,
            max_tokens,
            model_and_processor=model_and_processor,
            config=config,
            use_mlx=use_mlx,
        )
        markdown_pages.append(markdown)

    if return_type == "string":
        return "\n".join(markdown_pages)
    else:
        return markdown_pages
