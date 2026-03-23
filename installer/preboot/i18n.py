"""
EthOS Installer — Internationalisation (i18n).

Supports: pl, en, de, fr, es.  Polish is the default.
Usage:
    i18n = I18N()
    i18n.t("Wybierz język", lang="en")  →  "Choose language"
"""

_TRANSLATIONS = {
    # ── Step titles ──
    "Wybierz język": {
        "en": "Choose language",
        "de": "Sprache wählen",
        "fr": "Choisir la langue",
        "es": "Elegir idioma",
    },
    "Utwórz konto": {
        "en": "Create account",
        "de": "Konto erstellen",
        "fr": "Créer un compte",
        "es": "Crear cuenta",
    },
    "Wybierz dyski": {
        "en": "Choose disks",
        "de": "Festplatten wählen",
        "fr": "Choisir les disques",
        "es": "Elegir discos",
    },
    "Konfiguracja sieci": {
        "en": "Network setup",
        "de": "Netzwerk einrichten",
        "fr": "Configuration réseau",
        "es": "Configuración de red",
    },
    "Podsumowanie": {
        "en": "Summary",
        "de": "Zusammenfassung",
        "fr": "Résumé",
        "es": "Resumen",
    },
    "Instalacja": {
        "en": "Installation",
        "de": "Installation",
        "fr": "Installation",
        "es": "Instalación",
    },
    "Gotowe": {
        "en": "Done",
        "de": "Fertig",
        "fr": "Terminé",
        "es": "Listo",
    },

    # ── Language selection ──
    "Witaj w instalatorze EthOS": {
        "en": "Welcome to EthOS Installer",
        "de": "Willkommen beim EthOS-Installer",
        "fr": "Bienvenue dans l'installateur EthOS",
        "es": "Bienvenido al instalador de EthOS",
    },
    "Wybierz język instalacji": {
        "en": "Choose installation language",
        "de": "Installationssprache wählen",
        "fr": "Choisir la langue d'installation",
        "es": "Elegir idioma de instalación",
    },

    # ── Account creation ──
    "Nazwa użytkownika": {
        "en": "Username",
        "de": "Benutzername",
        "fr": "Nom d'utilisateur",
        "es": "Nombre de usuario",
    },
    "Hasło": {
        "en": "Password",
        "de": "Passwort",
        "fr": "Mot de passe",
        "es": "Contraseña",
    },
    "Powtórz hasło": {
        "en": "Confirm password",
        "de": "Passwort bestätigen",
        "fr": "Confirmer le mot de passe",
        "es": "Confirmar contraseña",
    },
    "Hasła nie są zgodne": {
        "en": "Passwords do not match",
        "de": "Passwörter stimmen nicht überein",
        "fr": "Les mots de passe ne correspondent pas",
        "es": "Las contraseñas no coinciden",
    },
    "Nazwa użytkownika jest wymagana": {
        "en": "Username is required",
        "de": "Benutzername ist erforderlich",
        "fr": "Le nom d'utilisateur est requis",
        "es": "El nombre de usuario es obligatorio",
    },
    "Hasło musi mieć min. 4 znaki": {
        "en": "Password must be at least 4 characters",
        "de": "Passwort muss mindestens 4 Zeichen haben",
        "fr": "Le mot de passe doit comporter au moins 4 caractères",
        "es": "La contraseña debe tener al menos 4 caracteres",
    },
    "Nazwa hosta": {
        "en": "Hostname",
        "de": "Hostname",
        "fr": "Nom d'hôte",
        "es": "Nombre de host",
    },

    # ── Disk selection ──
    "Dysk systemu": {
        "en": "System disk",
        "de": "Systemfestplatte",
        "fr": "Disque système",
        "es": "Disco del sistema",
    },
    "Dysk danych": {
        "en": "Data disk",
        "de": "Datenfestplatte",
        "fr": "Disque de données",
        "es": "Disco de datos",
    },
    "Ten sam dysk": {
        "en": "Same disk",
        "de": "Gleiche Festplatte",
        "fr": "Même disque",
        "es": "Mismo disco",
    },
    "Osobny dysk": {
        "en": "Separate disk",
        "de": "Separate Festplatte",
        "fr": "Disque séparé",
        "es": "Disco separado",
    },
    "Nie znaleziono dysków": {
        "en": "No disks found",
        "de": "Keine Festplatten gefunden",
        "fr": "Aucun disque trouvé",
        "es": "No se encontraron discos",
    },
    "UWAGA: Wszystkie dane na wybranych dyskach zostaną usunięte!": {
        "en": "WARNING: All data on selected disks will be erased!",
        "de": "ACHTUNG: Alle Daten auf den gewählten Festplatten werden gelöscht!",
        "fr": "ATTENTION: Toutes les données des disques sélectionnés seront effacées!",
        "es": "¡ATENCIÓN: Todos los datos en los discos seleccionados serán borrados!",
    },
    "Dysk startowy (USB)": {
        "en": "Boot disk (USB)",
        "de": "Boot-Datenträger (USB)",
        "fr": "Disque de démarrage (USB)",
        "es": "Disco de arranque (USB)",
    },

    # ── Network ──
    "Ethernet podłączony": {
        "en": "Ethernet connected",
        "de": "Ethernet verbunden",
        "fr": "Ethernet connecté",
        "es": "Ethernet conectado",
    },
    "Brak sieci — wybierz WiFi": {
        "en": "No network — choose WiFi",
        "de": "Kein Netzwerk — WiFi wählen",
        "fr": "Pas de réseau — choisissez le WiFi",
        "es": "Sin red — elige WiFi",
    },
    "Skanowanie sieci WiFi...": {
        "en": "Scanning WiFi networks...",
        "de": "WiFi-Netzwerke werden gesucht...",
        "fr": "Recherche des réseaux WiFi...",
        "es": "Buscando redes WiFi...",
    },
    "Hasło WiFi": {
        "en": "WiFi password",
        "de": "WiFi-Passwort",
        "fr": "Mot de passe WiFi",
        "es": "Contraseña WiFi",
    },
    "Łączenie...": {
        "en": "Connecting...",
        "de": "Verbindung wird hergestellt...",
        "fr": "Connexion...",
        "es": "Conectando...",
    },
    "Połączono z": {
        "en": "Connected to",
        "de": "Verbunden mit",
        "fr": "Connecté à",
        "es": "Conectado a",
    },
    "Nie udało się połączyć": {
        "en": "Failed to connect",
        "de": "Verbindung fehlgeschlagen",
        "fr": "Échec de la connexion",
        "es": "Error al conectar",
    },
    "Odśwież": {
        "en": "Refresh",
        "de": "Aktualisieren",
        "fr": "Actualiser",
        "es": "Actualizar",
    },

    # ── Summary / Install ──
    "Język": {
        "en": "Language",
        "de": "Sprache",
        "fr": "Langue",
        "es": "Idioma",
    },
    "Użytkownik": {
        "en": "User",
        "de": "Benutzer",
        "fr": "Utilisateur",
        "es": "Usuario",
    },
    "Sieć": {
        "en": "Network",
        "de": "Netzwerk",
        "fr": "Réseau",
        "es": "Red",
    },
    "Rozpocznij instalację": {
        "en": "Start installation",
        "de": "Installation starten",
        "fr": "Démarrer l'installation",
        "es": "Iniciar instalación",
    },
    "Wpisz INSTALUJ aby potwierdzić": {
        "en": "Type INSTALL to confirm",
        "de": "Tippe INSTALLIEREN zur Bestätigung",
        "fr": "Tapez INSTALLER pour confirmer",
        "es": "Escribe INSTALAR para confirmar",
    },
    "Instaluję system...": {
        "en": "Installing system...",
        "de": "System wird installiert...",
        "fr": "Installation du système...",
        "es": "Instalando sistema...",
    },
    "Partycjonowanie dysków": {
        "en": "Partitioning disks",
        "de": "Festplatten werden partitioniert",
        "fr": "Partitionnement des disques",
        "es": "Particionando discos",
    },
    "Kopiowanie systemu": {
        "en": "Copying system",
        "de": "System wird kopiert",
        "fr": "Copie du système",
        "es": "Copiando sistema",
    },
    "Konfiguracja bootloadera": {
        "en": "Configuring bootloader",
        "de": "Bootloader wird konfiguriert",
        "fr": "Configuration du chargeur de démarrage",
        "es": "Configurando cargador de arranque",
    },
    "Tworzenie użytkownika": {
        "en": "Creating user",
        "de": "Benutzer wird erstellt",
        "fr": "Création de l'utilisateur",
        "es": "Creando usuario",
    },
    "Finalizacja": {
        "en": "Finalizing",
        "de": "Abschluss",
        "fr": "Finalisation",
        "es": "Finalizando",
    },

    # ── Done ──
    "Instalacja zakończona pomyślnie!": {
        "en": "Installation completed successfully!",
        "de": "Installation erfolgreich abgeschlossen!",
        "fr": "Installation terminée avec succès!",
        "es": "¡Instalación completada con éxito!",
    },
    "System zostanie uruchomiony ponownie...": {
        "en": "System will restart...",
        "de": "System wird neu gestartet...",
        "fr": "Le système va redémarrer...",
        "es": "El sistema se reiniciará...",
    },
    "Uruchom ponownie": {
        "en": "Restart",
        "de": "Neustart",
        "fr": "Redémarrer",
        "es": "Reiniciar",
    },
    "Po restarcie połącz się z EthOS pod adresem:": {
        "en": "After restart, connect to EthOS at:",
        "de": "Nach dem Neustart verbinden Sie sich mit EthOS unter:",
        "fr": "Après le redémarrage, connectez-vous à EthOS à:",
        "es": "Después del reinicio, conéctese a EthOS en:",
    },

    # ── Buttons ──
    "Dalej": {
        "en": "Next",
        "de": "Weiter",
        "fr": "Suivant",
        "es": "Siguiente",
    },
    "Wstecz": {
        "en": "Back",
        "de": "Zurück",
        "fr": "Retour",
        "es": "Atrás",
    },
    "Anuluj": {
        "en": "Cancel",
        "de": "Abbrechen",
        "fr": "Annuler",
        "es": "Cancelar",
    },

    # ── Misc ──
    "Błąd": {
        "en": "Error",
        "de": "Fehler",
        "fr": "Erreur",
        "es": "Error",
    },
    "Spróbuj ponownie": {
        "en": "Try again",
        "de": "Erneut versuchen",
        "fr": "Réessayer",
        "es": "Intentar de nuevo",
    },
    "Ładowanie...": {
        "en": "Loading...",
        "de": "Laden...",
        "fr": "Chargement...",
        "es": "Cargando...",
    },
}

# Confirmation tokens per language
CONFIRM_TOKENS = {
    "pl": "INSTALUJ",
    "en": "INSTALL",
    "de": "INSTALLIEREN",
    "fr": "INSTALLER",
    "es": "INSTALAR",
}

LANGUAGE_LABELS = {
    "pl": "Polski",
    "en": "English",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
}


class I18N:
    """Simple translation helper."""

    def __init__(self):
        self._tr = _TRANSLATIONS

    def t(self, key, lang="pl"):
        """Translate *key* to *lang*. Falls back to the key itself (Polish)."""
        if lang == "pl":
            return key
        entry = self._tr.get(key)
        if entry and lang in entry:
            return entry[lang]
        return key

    def get_all(self, lang="pl"):
        """Return full translation dict for a language (for frontend JS)."""
        if lang == "pl":
            return {k: k for k in self._tr}
        return {k: v.get(lang, k) for k, v in self._tr.items()}

    def available_languages(self):
        """Return list of supported languages with labels."""
        return [{"code": c, "label": l} for c, l in LANGUAGE_LABELS.items()]

    def confirm_token(self, lang="pl"):
        """Return the confirmation word for a language."""
        return CONFIRM_TOKENS.get(lang, "INSTALUJ")
