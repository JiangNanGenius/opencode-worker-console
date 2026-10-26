"""Enable link-time optimization after Arduino adds its default -fno-lto."""
Import("env")
env.Replace(LINKFLAGS=[flag for flag in env.get("LINKFLAGS", []) if flag != "-fno-lto"])
env.Append(LINKFLAGS=["-flto"])
