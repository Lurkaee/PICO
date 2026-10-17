import uuid


def generate_id(prefix: str) -> str:
    """Generate a clean, collision-resistant prefixed identifier.

    Examples:
        generate_id("conv") -> "conv_3f8a9e2d1c0b4a78"
    """
    clean_uuid = uuid.uuid4().hex[:16]
    return f"{prefix}_{clean_uuid}"
