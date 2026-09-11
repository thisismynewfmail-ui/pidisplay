Put GGUF model files in this directory.

They are found automatically, including in subdirectories, and appear in the
model picker (F3). For a multi-part model, place every shard here; only the
first (`-00001-of-*.gguf`) is listed, and llama.cpp loads the rest itself.

Sizing, for a Raspberry Pi 5 with 8 GB:

    1-2 B parameters, Q4_K_M   fast, roughly 8-15 tokens/second
    3-4 B parameters, Q4_K_M   comfortable, roughly 4-7 tokens/second
    7-8 B parameters, Q4_K_M   usable but slow, roughly 2-3 tokens/second

Leave at least 1.5 GB free beyond the file size for the KV cache and the rest
of the system. The context setting is what spends that: 4096 tokens is a
reasonable default, and the terminal will refuse to exceed what the model was
actually trained for.
