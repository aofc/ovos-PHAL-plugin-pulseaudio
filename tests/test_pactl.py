"""Lecture du volume et du muet dans la sortie de `pactl list sinks`.

Le piège : « Base Volume: 65536 / 100% », qui suit « Volume: … 50% » dans la sortie de
pactl, écrasait le vrai volume — on lisait 100 quoi qu'on règle, donc Jeedom aussi.

    python3 tests/test_pactl.py
"""
import os
import subprocess
import sys
import types

# Le module importe OVOS ; on n'en a pas besoin ici, seulement de PulseAudio.update().
for nom, attrs in (("json_database", {"JsonConfigXDG": object}),
                   ("ovos_bus_client", {"Message": object}),
                   ("ovos_plugin_manager", {}),
                   ("ovos_plugin_manager.phal", {"PHALPlugin": object}),
                   ("ovos_utils", {}),
                   ("ovos_utils.system", {"find_executable": lambda e: None,
                                          "is_process_running": lambda e: False})):
    m = types.ModuleType(nom)
    m.__dict__.update(attrs)
    sys.modules[nom] = m
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from deuxcentdeuxhome_phal_plugin_pulseaudio import PulseAudio  # noqa: E402

SORTIE = """Sink #0
\tState: RUNNING
\tName: alsa_output.platform-soc_sound.stereo-fallback
\tDescription: Built-in Audio Stereo
\tDriver: module-alsa-card.c
\tMute: no
\tVolume: front-left: 32768 /  50% / -18.06 dB,   front-right: 32768 /  50% / -18.06 dB
\t        balance 0.00
\tBase Volume: 65536 / 100% / 0.00 dB
\tMonitor Source: alsa_output.platform-soc_sound.stereo-fallback.monitor
\tFlags: HARDWARE HW_MUTE_CTRL HW_VOLUME_CTRL DECIBEL_VOLUME LATENCY SET_FORMATS
\tProperties:
\t\talsa.resolution_bits = "16"

Sink #1
\tState: IDLE
\tName: hdmi_output
\tMute: yes
\tVolume: front-left: 13107 /  20% / -41.94 dB,   front-right: 13107 /  20% / -41.94 dB
\tBase Volume: 65536 / 100% / 0.00 dB
"""

echecs, total = [], 0


def verifier(nom, obtenu, attendu):
    global total
    total += 1
    if obtenu == attendu:
        print(f"  ok  {nom}")
    else:
        echecs.append(nom)
        print(f"  ÉCHEC : {nom}: attendu {attendu!r}, obtenu {obtenu!r}")


appels = []


def faux_run(cmd, **kw):
    appels.append((cmd, kw))
    return types.SimpleNamespace(stdout=SORTIE, returncode=0)


subprocess.run = faux_run
p = PulseAudio()
verifier("volume du premier sink : 50, PAS le Base Volume (100)",
         p.get_volume_percent(), 50)
verifier("...et celui du second sink", p._volume["hdmi_output"], 20)
verifier("muet du premier sink : non", p.get_mute(), False)
verifier("muet du second sink : oui", p._mute["hdmi_output"], True)
verifier("pactl est appelé avec LC_ALL=C (étiquettes stables)",
         appels[0][1].get("env", {}).get("LC_ALL"), "C")

# Régler puis relire donne la valeur réglée (la relecture ne retombe plus sur 100).
p.set_volume_percent(35)
p._volume.clear()
SORTIE_35 = SORTIE.replace("50%", "35%")
subprocess.run = lambda cmd, **kw: types.SimpleNamespace(stdout=SORTIE_35, returncode=0)
verifier("après réglage à 35, la relecture rend 35", p.get_volume_percent(), 35)
# Augmenter : la base est la valeur COURANTE, pas 100.
sets = []
subprocess.run = lambda cmd, **kw: (sets.append(cmd), types.SimpleNamespace(stdout=SORTIE_35, returncode=0))[1]
p.increase_volume(10)
verifier("augmenter de 10 depuis 35 règle 45 (avant : 100 + 10)",
         [c for c in sets if "set-sink-volume" in c][-1][-1], "45%")
p.decrease_volume(10)
verifier("diminuer de 10 depuis 35 règle 25",
         [c for c in sets if "set-sink-volume" in c][-1][-1], "25%")

print()
if echecs:
    print(f"{len(echecs)} ÉCHEC(S) sur {total}.")
    sys.exit(1)
print(f"{total} contrôles passés, 0 échec(s).")
