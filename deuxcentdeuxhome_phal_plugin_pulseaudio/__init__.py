import collections
import os
import re
import subprocess

from json_database import JsonConfigXDG
from ovos_bus_client import Message
from ovos_plugin_manager.phal import PHALPlugin
from ovos_utils.system import find_executable, is_process_running


class PulseAudioValidator:
    @staticmethod
    def validate(config=None):
        """Cette méthode est appelée avant le chargement du plugin."""
        execs = ["pactl", "pulseaudio", "pipewire"]
        return any((find_executable(e) or is_process_running(e) for e in execs))


class PulseAudioVolumeControlPlugin(PHALPlugin):
    validator = PulseAudioValidator

    def __init__(self, bus=None, config=None):
        super().__init__(bus=bus, name="202home-phal-plugin-pulseaudio", config=config)
        self.settings = JsonConfigXDG(self.name, subfolder="OpenVoiceOS")
        self.pulseaudio = PulseAudio()
        self.bus.on("mycroft.volume.get", self.handle_volume_request)
        self.bus.on("mycroft.volume.set", self.handle_volume_change)
        self.bus.on("mycroft.volume.increase", self.handle_volume_increase)
        self.bus.on("mycroft.volume.decrease", self.handle_volume_decrease)
        self.bus.on("mycroft.volume.set.gui", self.handle_volume_change_gui)
        self.bus.on("mycroft.volume.mute", self.handle_mute_request)
        self.bus.on("mycroft.volume.unmute", self.handle_unmute_request)
        self.bus.on("mycroft.volume.mute.toggle", self.handle_mute_toggle_request)

        self.bus.on("mycroft.volume.get.sliding.panel", self.handle_volume_request)

        if self.settings.get("first_boot", True):
            self.set_volume(50)
            self.settings["first_boot"] = False
            self.settings.store()

    def get_volume(self):
        return self.pulseaudio.get_volume_percent()

    def set_volume(self, percent=None, set_by_gui=False, play_sound=True):
        if percent is None:
            return
        volume = int(percent)
        volume = min(100, max(0, volume))
        self.pulseaudio.set_volume_percent(volume)
        if play_sound:
            self.bus.emit(Message("mycroft.audio.play_sound", {"uri": "snd/blop-mark-diangelo.wav"}))
        if not set_by_gui:
            percent_val = volume / 100
            self.handle_volume_request(
                Message("mycroft.volume.get", {"percent": percent_val}))

    def increase_volume(self, volume_change=None, play_sound=True):
        if not volume_change:
            volume_change = 15
        self.pulseaudio.increase_volume(volume_change)
        if play_sound:
            self.bus.emit(Message("mycroft.audio.play_sound", {"uri": "snd/blop-mark-diangelo.wav"}))
        self.handle_volume_request(Message("mycroft.volume.get"))

    def decrease_volume(self, volume_change=None, play_sound=True):
        if not volume_change:
            volume_change = 15
        self.pulseaudio.decrease_volume(volume_change)
        if play_sound:
            self.bus.emit(Message("mycroft.audio.play_sound", {"uri": "snd/blop-mark-diangelo.wav"}))
        self.handle_volume_request(Message("mycroft.volume.get"))

    def handle_mute_request(self, message):
        self.log.info("User muted audio.")
        self.pulseaudio.set_mute(True)
        self.bus.emit(Message("mycroft.volume.get").response({"percent": 0}))

    def handle_unmute_request(self, message):
        self.log.info("User unmuted audio.")
        self.pulseaudio.set_mute(False)
        volume = self.pulseaudio.get_volume_percent()
        self.bus.emit(Message("mycroft.volume.get").response({"percent": volume / 100}))

    def handle_mute_toggle_request(self, message):
        muted = not self.pulseaudio.get_mute()
        self.pulseaudio.set_mute(muted)
        self.log.info(f"User toggled mute. Result: {'muted' if muted else 'unmuted'}")
        self.bus.emit(Message("mycroft.volume.get").response(
            {"percent": 0 if muted else (self.pulseaudio.get_volume_percent() / 100)}))

    def handle_volume_request(self, message):
        percent = self.get_volume() / 100
        self.bus.emit(message.response({"percent": percent}))

    def handle_volume_change(self, message):
        percent = message.data.get("percent", 0.5) * 100
        play_sound = message.data.get("play_sound", True)
        assert isinstance(play_sound, bool)
        self.set_volume(percent, play_sound=play_sound)

    def handle_volume_increase(self, message):
        percent = message.data.get("percent", .10) * 100
        play_sound = message.data.get("play_sound", True)
        assert isinstance(play_sound, bool)
        self.increase_volume(percent, play_sound)

    def handle_volume_decrease(self, message):
        percent = message.data.get("percent", .10) * 100
        play_sound = message.data.get("play_sound", True)
        assert isinstance(play_sound, bool)
        self.decrease_volume(percent, play_sound)

    def handle_volume_change_gui(self, message):
        percent = message.data.get("percent", 0.5) * 100
        play_sound = message.data.get("play_sound", True)
        assert isinstance(play_sound, bool)
        self.set_volume(percent, set_by_gui=True, play_sound=play_sound)

    def shutdown(self):
        self.bus.remove("mycroft.volume.get", self.handle_volume_request)
        self.bus.remove("mycroft.volume.set", self.handle_volume_change)
        self.bus.remove("mycroft.volume.increase", self.handle_volume_increase)
        self.bus.remove("mycroft.volume.decrease", self.handle_volume_decrease)
        self.bus.remove("mycroft.volume.set.gui", self.handle_volume_change_gui)
        self.bus.remove("mycroft.volume.mute", self.handle_mute_request)
        self.bus.remove("mycroft.volume.unmute", self.handle_unmute_request)
        self.bus.remove("mycroft.volume.mute.toggle", self.handle_mute_toggle_request)
        super().shutdown()


class PulseAudio:
    """Gestionnaire PulseAudio / PipeWire utilisant pactl."""

    def __init__(self):
        self._mute = collections.OrderedDict()
        self._volume = collections.OrderedDict()
        self.update()

    def update(self):
        """Récupère la liste des sinks, leur volume et statut mute via pactl."""
        try:
            # LC_ALL=C : les étiquettes (« Volume: », « Mute: », « Name: ») ne dépendent
            # plus de la langue du système ; l'analyse ci-dessous les attend en anglais.
            res = subprocess.run(
                ['pactl', 'list', 'sinks'],
                capture_output=True, text=True, check=True,
                env={**os.environ, "LC_ALL": "C"}
            )
            out = res.stdout
        except (subprocess.SubprocessError, FileNotFoundError):
            return

        current_sink = None
        sinks_vol = collections.OrderedDict()
        sinks_mute = collections.OrderedDict()

        for line in out.splitlines():
            line_str = line.strip()
            if line_str.startswith("Name: ") or line_str.startswith("Nom : "):
                current_sink = line_str.split(": ", 1)[1]
            elif current_sink:
                # `^Volume\s*:` — la ligne du volume COURANT, et elle seule. Le test
                # « "Volume:" in ligne » prenait aussi « Base Volume: 65536 / 100% »,
                # qui vient APRÈS dans la sortie de pactl et écrasait le vrai volume :
                # on lisait toujours 100, quoi qu'on règle.
                if re.match(r'^Volume\s*:', line_str):
                    # Recherche du pourcentage ex: "50%" (le premier : canal avant-gauche)
                    match = re.search(r'(\d+)%', line_str)
                    if match:
                        sinks_vol[current_sink] = int(match.group(1))
                elif re.match(r'^(Mute|Sourdine)\s*:', line_str):
                    is_muted = "yes" in line_str.lower() or "oui" in line_str.lower()
                    sinks_mute[current_sink] = is_muted

        if sinks_vol:
            self._volume = sinks_vol
        if sinks_mute:
            self._mute = sinks_mute

    def _get_target_sink(self, sink=None, data_dict=None):
        if sink:
            return sink
        if data_dict and len(data_dict) > 0:
            return list(data_dict.keys())[0]
        # Fallback si aucun sink n'est enregistré dans l'index
        return "@DEFAULT_SINK@"

    def get_volume_percent(self, sink=None):
        self.update()
        target = self._get_target_sink(sink, self._volume)
        return self._volume.get(target, 50)

    def get_mute(self, sink=None):
        self.update()
        target = self._get_target_sink(sink, self._mute)
        return self._mute.get(target, False)

    def set_mute(self, mute, sink=None):
        target = self._get_target_sink(sink, self._mute)
        val = '1' if mute else '0'
        subprocess.run(
            ['pactl', 'set-sink-mute', target, val],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if target in self._mute:
            self._mute[target] = mute

    def set_volume_percent(self, percent, sink=None):
        target = self._get_target_sink(sink, self._volume)
        percent = min(100, max(0, int(percent)))
        subprocess.run(
            ['pactl', 'set-sink-volume', target, f'{percent}%'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if target in self._volume:
            self._volume[target] = percent

    def increase_volume(self, percent):
        vol = self.get_volume_percent() + percent
        self.set_volume_percent(vol)

    def decrease_volume(self, percent):
        vol = self.get_volume_percent() - percent
        self.set_volume_percent(vol)


if __name__ == "__main__":
    p = PulseAudio()
    print("Volume actuel (%):", p.get_volume_percent())
    print("Mute:", p.get_mute())