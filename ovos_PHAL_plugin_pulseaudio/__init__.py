import collections
import re
import subprocess
from os.path import join, dirname

from json_database import JsonConfigXDG
from ovos_bus_client import Message
from ovos_plugin_manager.phal import PHALPlugin
from ovos_utils.system import find_executable, is_process_running


class PulseAudioValidator:
    @staticmethod
    def validate(config=None):
        """ this method is called before loading the plugin.
        If it returns False the plugin is not loaded.
        This allows a plugin to run platform checks"""
        # any aliases we need here ?
        execs = ["pulseaudio"]
        return any((find_executable(e) or is_process_running(e)
                    for e in execs))


class PulseAudioVolumeControlPlugin(PHALPlugin):
    validator = PulseAudioValidator

    def __init__(self, bus=None, config=None):
        super().__init__(bus=bus, name="ovos-PHAL-plugin-pulseaudio", config=config)
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

        # A silent method to get the volume without invoking the shell osd
        # Needed as gui will always refresh and request it
        # When sliding panel opens to refresh volume value data
        self.bus.on("mycroft.volume.get.sliding.panel", self.handle_volume_request)

        if self.settings.get("first_boot", True):
            self.set_volume(50)
            self.settings["first_boot"] = False
            self.settings.store()

    def get_volume(self):
        return self.pulseaudio.get_volume_percent()

    def set_volume(self, percent=None,
                         set_by_gui=False,
                         play_sound=True):
        volume = int(percent)
        volume = min(100, volume)
        volume = max(0, volume)
        self.pulseaudio.set_volume_percent(volume)
        if play_sound:
            self.bus.emit(Message("mycroft.audio.play_sound", {"uri": "snd/blop-mark-diangelo.wav"}))
        # report change to GUI
        if not set_by_gui:
            percent = volume / 100
            self.handle_volume_request(
                Message("mycroft.volume.get", {"percent": percent}))

    def increase_volume(self, volume_change=None,
                              play_sound=True):
        if not volume_change:
            volume_change = 15
        self.pulseaudio.increase_volume(volume_change)
        if play_sound:
            self.bus.emit(Message("mycroft.audio.play_sound", {"uri": "snd/blop-mark-diangelo.wav"}))
        self.handle_volume_request(Message("mycroft.volume.get"))

    def decrease_volume(self, volume_change=None,
                              play_sound=True):
        if not volume_change:
            volume_change = -15
        if volume_change > 0:
            volume_change = 0 - volume_change
        self.pulseaudio.increase_volume(volume_change)
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
        percent = message.data["percent"] * 100
        play_sound = message.data.get("play_sound", True)
        assert isinstance(play_sound, bool)
        self.set_volume(percent, play_sound=play_sound)

    def handle_volume_increase(self, message):
        percent = message.data.get("percent", .10) * 100
        play_sound = message.data.get("play_sound", True)
        assert isinstance(play_sound, bool)
        self.increase_volume(percent, play_sound)

    def handle_volume_decrease(self, message):
        percent = message.data.get("percent", -.10) * 100
        play_sound = message.data.get("play_sound", True)
        assert isinstance(play_sound, bool)
        self.decrease_volume(percent, play_sound)

    def handle_volume_change_gui(self, message):
        percent = message.data["percent"] * 100
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
    volume_re = re.compile('^set-sink-volume ([^ ]+) (.*)')
    mute_re = re.compile('^set-sink-mute ([^ ]+) ((?:yes)|(?:no))')

    def __init__(self):
        # 202home : update() ici scanne PulseAudio (`pacmd dump`) UNE SEULE
        # FOIS, à la construction — jamais rejoué ensuite nulle part dans
        # l'amont. Si ce scan tombe avant que PulseAudio n'ait fini de
        # créer sa sortie combinée, self._volume/self._mute restent VIDES
        # POUR TOUJOURS : get_sink_volume()/set_sink_volume()/get_mute()/
        # set_mute() font tous list(self._volume_ou_mute.keys())[0], qui
        # lève IndexError sans jamais se rattraper — ni la lecture ni le
        # réglage du volume ne refonctionnent avant un redémarrage complet
        # du service. Constaté en vrai. Chacune de ces quatre méthodes
        # rescanne maintenant elle-même si son dictionnaire est encore
        # vide, au moment où on en a besoin plutôt qu'une seule fois ici.
        self._mute = collections.OrderedDict()
        self._volume = collections.OrderedDict()
        self.update()

    def normalize_sinks(self):
        self.unmute_all()
        volume = self.get_volume()
        self.set_all_volumes(volume)

    def update(self):
        proc = subprocess.Popen(['pacmd', 'dump'], stdout=subprocess.PIPE)

        for line in proc.stdout:
            line = line.decode("utf-8")
            volume_match = PulseAudio.volume_re.match(line)
            mute_match = PulseAudio.mute_re.match(line)

            if volume_match:
                self._volume[volume_match.group(1)] = int(
                    volume_match.group(2), 16)
            elif mute_match:
                self._mute[mute_match.group(1)] = mute_match.group(
                    2).lower() == "yes"

    def _vol_to_percent(self, vol):
        max_vol = 65536
        percent = vol * 100 / max_vol
        return percent

    def _percent_to_vol(self, percent):
        max_vol = 65536
        vol = percent * max_vol / 100
        return vol

    def get_volume_percent(self, sink=None):
        vol = self.get_sink_volume(sink)
        return self._vol_to_percent(vol)

    def get_mute(self, sink=None):
        # 202home : rescan si _mute est encore vide — voir le commentaire
        # de __init__ sur la course avec la création de la sortie combinée.
        if not self._mute:
            self.update()
        if not sink:
            sink = list(self._mute.keys())[0]

        return self._mute[sink]

    def get_volume(self, sink=None):
        return self.get_sink_volume(sink)

    def get_sink_volume(self, sink=None):
        # 202home : même rescan que get_mute(), même raison.
        if not self._volume:
            self.update()
        if not sink:
            sink = list(self._volume.keys())[0]

        return self._volume[sink]

    def set_mute(self, mute, sink=None):
        # 202home : même rescan que get_mute()/get_sink_volume().
        if not self._mute:
            self.update()
        if not sink:
            sink = list(self._mute.keys())[0]

        subprocess.Popen(
            ['pacmd', 'set-sink-mute', sink, 'yes' if mute else 'no'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._mute[sink] = mute

    def set_volume(self, volume, sink=None):
        self.set_sink_volume(volume, sink)

    def set_volume_percent(self, volume, sink=None):
        self.set_sink_volume(self._percent_to_vol(volume), sink)

    def set_sink_volume(self, volume, sink=None):
        # 202home : même rescan que get_mute()/get_sink_volume().
        if not self._volume:
            self.update()
        if not sink:
            sink = list(self._volume.keys())[0]
        volume = int(volume)
        subprocess.Popen(['pacmd', 'set-sink-volume', sink, hex(volume)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._volume[sink] = volume

    def mute_all(self):
        for sink in self.list_sinks():
            self.set_mute(True, sink)

    def unmute_all(self):
        for sink in self.list_sinks():
            self.set_mute(False, sink)

    def set_all_volumes(self, volume):
        self.set_all_sink_volumes(volume)

    def get_all_volumes(self):
        return self.get_all_sink_volumes()

    def set_all_volumes_percent(self, percent):
        volume = self._percent_to_vol(percent)
        self.set_all_sink_volumes(volume)

    def get_all_volumes_percent(self):
        return [self._vol_to_percent(volume) for volume in
                self.get_all_sink_volumes()]

    def set_all_sink_volumes(self, volume):
        for sink in self.list_sinks():
            self.set_volume(volume, sink)

    def get_all_sink_volumes(self):
        volumes = []
        for sink in self.list_sinks():
            volumes.append(self.get_volume(sink))
        return volumes

    def list_sinks(self):
        proc = subprocess.Popen(['pacmd', 'list-sinks'],
                                stdout=subprocess.PIPE)
        sinks = []
        for line in proc.stdout:
            line = line.decode("utf-8").strip()
            if line.startswith("name: <"):
                sink = line.replace("name: <", "")[:-1]
                sinks.append(sink)
        return sinks

    def list_sources(self):
        proc = subprocess.Popen(['pacmd', 'list-sources'],
                                stdout=subprocess.PIPE)
        sinks = []
        for line in proc.stdout:
            line = line.decode("utf-8").strip()
            if line.startswith("name: <"):
                sink = line.replace("name: <", "")[:-1]
                sinks.append(sink)
        return sinks

    def increase_volume(self, percent):
        volume = self.get_volume_percent()
        volume += percent
        if volume < 0:
            volume = 0
        elif volume > 100:
            volume = 100
        self.set_all_volumes_percent(volume)

    def decrease_volume(self, percent):
        volume = self.get_volume_percent()
        volume -= percent
        if volume < 0:
            volume = 0
        elif volume > 100:
            volume = 100
        self.set_all_volumes_percent(volume)


if __name__ == "__main__":
    p = PulseAudio()
    print(p.list_sources())
