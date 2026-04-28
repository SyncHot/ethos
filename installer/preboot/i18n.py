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

    # ── Scenario selection ──
    "Tryb instalacji": {
        "en": "Installation mode",
        "de": "Installationsmodus",
        "fr": "Mode d'installation",
        "es": "Modo de instalación",
    },
    "Wybierz jak zainstalować system": {
        "en": "Choose how to install the system",
        "de": "Wähle die Installationsart",
        "fr": "Choisissez comment installer le système",
        "es": "Elige cómo instalar el sistema",
    },
    "Analizuję dyski...": {
        "en": "Analyzing disks...",
        "de": "Festplatten werden analysiert...",
        "fr": "Analyse des disques...",
        "es": "Analizando discos...",
    },
    "Prosty": {
        "en": "Simple",
        "de": "Einfach",
        "fr": "Simple",
        "es": "Simple",
    },
    "System i dane na jednym dysku": {
        "en": "System and data on one disk",
        "de": "System und Daten auf einer Festplatte",
        "fr": "Système et données sur un disque",
        "es": "Sistema y datos en un disco",
    },
    "Jeden dysk na wszystko. Idealne do rozpoczęcia.": {
        "en": "One disk for everything. Perfect to get started.",
        "de": "Eine Festplatte für alles. Ideal zum Starten.",
        "fr": "Un disque pour tout. Idéal pour commencer.",
        "es": "Un disco para todo. Ideal para empezar.",
    },
    "Wydajny": {
        "en": "Performance",
        "de": "Leistung",
        "fr": "Performance",
        "es": "Rendimiento",
    },
    "Szybki SSD na system, HDD na dane": {
        "en": "Fast SSD for system, HDD for data",
        "de": "Schnelle SSD fürs System, HDD für Daten",
        "fr": "SSD rapide pour le système, HDD pour les données",
        "es": "SSD rápido para el sistema, HDD para los datos",
    },
    "System na szybkim dysku, dane na dużym. Najlepsza wydajność.": {
        "en": "System on fast disk, data on large disk. Best performance.",
        "de": "System auf schneller Festplatte, Daten auf großer. Beste Leistung.",
        "fr": "Système sur disque rapide, données sur grand disque. Meilleures performances.",
        "es": "Sistema en disco rápido, datos en grande. Mejor rendimiento.",
    },
    "Zaawansowany": {
        "en": "Advanced",
        "de": "Erweitert",
        "fr": "Avancé",
        "es": "Avanzado",
    },
    "Ręczny wybór dysków": {
        "en": "Manual disk selection",
        "de": "Manuelle Festplattenauswahl",
        "fr": "Sélection manuelle des disques",
        "es": "Selección manual de discos",
    },
    "Pełna kontrola nad przypisaniem dysków do ról.": {
        "en": "Full control over disk role assignment.",
        "de": "Volle Kontrolle über die Zuweisung der Festplattenrollen.",
        "fr": "Contrôle total sur l'attribution des rôles des disques.",
        "es": "Control total sobre la asignación de roles de disco.",
    },
    "Zalecane": {
        "en": "Recommended",
        "de": "Empfohlen",
        "fr": "Recommandé",
        "es": "Recomendado",
    },
    "Potrzeba ≥2 dyski": {
        "en": "Requires ≥2 disks",
        "de": "Benötigt ≥2 Festplatten",
        "fr": "Nécessite ≥2 disques",
        "es": "Requiere ≥2 discos",
    },
    "Wybierz tryb instalacji": {
        "en": "Select installation mode",
        "de": "Installationsmodus auswählen",
        "fr": "Sélectionner le mode d'installation",
        "es": "Seleccionar modo de instalación",
    },

    # ── Disk visualization ──
    "Układ partycji": {
        "en": "Partition layout",
        "de": "Partitionslayout",
        "fr": "Disposition des partitions",
        "es": "Diseño de particiones",
    },
    "Dane": {
        "en": "Data",
        "de": "Daten",
        "fr": "Données",
        "es": "Datos",
    },
    "System": {
        "en": "System",
        "de": "System",
        "fr": "Système",
        "es": "Sistema",
    },
    "partycji": {
        "en": "partitions",
        "de": "Partitionen",
        "fr": "partitions",
        "es": "particiones",
    },
    "Instalator": {
        "en": "Installer",
        "de": "Installer",
        "fr": "Installateur",
        "es": "Instalador",
    },
    "Dysk systemu i danych nie mogą być takie same": {
        "en": "System and data disk cannot be the same",
        "de": "System- und Datenfestplatte können nicht identisch sein",
        "fr": "Le disque système et données ne peuvent pas être identiques",
        "es": "El disco del sistema y de datos no pueden ser iguales",
    },
    "jeden dysk": {
        "en": "one disk",
        "de": "eine Festplatte",
        "fr": "un disque",
        "es": "un disco",
    },
    "SSD + HDD": {
        "en": "SSD + HDD",
        "de": "SSD + HDD",
        "fr": "SSD + HDD",
        "es": "SSD + HDD",
    },
    "Tryb": {
        "en": "Mode",
        "de": "Modus",
        "fr": "Mode",
        "es": "Modo",
    },

    # ── Security / encryption ──
    "Bezpieczeństwo": {
        "en": "Security",
        "de": "Sicherheit",
        "fr": "Sécurité",
        "es": "Seguridad",
    },
    "Szyfrowanie danych (LUKS)": {
        "en": "Data encryption (LUKS)",
        "de": "Datenverschlüsselung (LUKS)",
        "fr": "Chiffrement des données (LUKS)",
        "es": "Cifrado de datos (LUKS)",
    },
    "Chroni dane przed nieautoryzowanym dostępem fizycznym": {
        "en": "Protects data from unauthorized physical access",
        "de": "Schützt Daten vor unbefugtem physischem Zugriff",
        "fr": "Protège les données contre l'accès physique non autorisé",
        "es": "Protege los datos del acceso físico no autorizado",
    },
    "Hasło awaryjne": {
        "en": "Recovery passphrase",
        "de": "Notfall-Passwort",
        "fr": "Mot de passe de récupération",
        "es": "Contraseña de recuperación",
    },
    "Powtórz hasło awaryjne": {
        "en": "Confirm recovery passphrase",
        "de": "Notfall-Passwort bestätigen",
        "fr": "Confirmer le mot de passe de récupération",
        "es": "Confirmar contraseña de recuperación",
    },
    "Hasło musi mieć min. 8 znaków": {
        "en": "Passphrase must be at least 8 characters",
        "de": "Passwort muss mindestens 8 Zeichen haben",
        "fr": "Le mot de passe doit comporter au moins 8 caractères",
        "es": "La contraseña debe tener al menos 8 caracteres",
    },
    "Dysk odblokuje się automatycznie przy starcie. Hasło awaryjne służy do ręcznego odzyskiwania danych.": {
        "en": "The disk unlocks automatically at boot. The recovery passphrase is for manual data recovery.",
        "de": "Die Festplatte wird beim Start automatisch entsperrt. Das Notfall-Passwort dient zur manuellen Datenwiederherstellung.",
        "fr": "Le disque se déverrouille automatiquement au démarrage. Le mot de passe de récupération sert à la récupération manuelle des données.",
        "es": "El disco se desbloquea automáticamente al arrancar. La contraseña de recuperación es para recuperación manual de datos.",
    },
    "Weryfikacja integralności": {
        "en": "Integrity verification",
        "de": "Integritätsprüfung",
        "fr": "Vérification d'intégrité",
        "es": "Verificación de integridad",
    },
    "Immutable root (SquashFS) chroni system przed modyfikacją. Zawsze włączone.": {
        "en": "Immutable root (SquashFS) protects the system from modification. Always enabled.",
        "de": "Immutable Root (SquashFS) schützt das System vor Änderungen. Immer aktiviert.",
        "fr": "Root immuable (SquashFS) protège le système contre les modifications. Toujours activé.",
        "es": "Root inmutable (SquashFS) protege el sistema contra modificaciones. Siempre activo.",
    },
    "Szyfrowanie": {
        "en": "Encryption",
        "de": "Verschlüsselung",
        "fr": "Chiffrement",
        "es": "Cifrado",
    },
    "Wyłączone": {
        "en": "Disabled",
        "de": "Deaktiviert",
        "fr": "Désactivé",
        "es": "Desactivado",
    },
    "System zainstalowany!": {
        "en": "System installed!",
        "de": "System installiert!",
        "fr": "Système installé !",
        "es": "¡Sistema instalado!",
    },
    "Wybierz dysk systemu": {
        "en": "Select system disk",
        "de": "Systemfestplatte auswählen",
        "fr": "Sélectionner le disque système",
        "es": "Seleccionar disco del sistema",
    },
    "Wybierz dysk danych": {
        "en": "Select data disk",
        "de": "Datenfestplatte auswählen",
        "fr": "Sélectionner le disque de données",
        "es": "Seleccionar disco de datos",
    },
    "USB — wolniejszy": {
        "en": "USB — slower",
        "de": "USB — langsamer",
        "fr": "USB — plus lent",
        "es": "USB — más lento",
    },
    "Szukaj EthOS w sieci": {
        "en": "Look for EthOS on the network",
        "de": "Suche EthOS im Netzwerk",
        "fr": "Chercher EthOS sur le réseau",
        "es": "Buscar EthOS en la red",
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
