import 'package:flutter/material.dart';

class SettingsDialog extends StatefulWidget {
  final void Function(String theme, bool notifications) onSave;

  const SettingsDialog({super.key, required this.onSave});

  @override
  State<SettingsDialog> createState() => _SettingsDialogState();
}

class _SettingsDialogState extends State<SettingsDialog> {
  String _selectedTheme = 'System';
  bool _notificationsEnabled = true;
  final List<String> _themes = ['Hell', 'Dunkel', 'System'];

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Einstellungen'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          // Theme-Auswahl
          DropdownButtonFormField<String>(
            value: _selectedTheme,
            decoration: const InputDecoration(labelText: 'Theme'),
            items: _themes.map((t) => DropdownMenuItem(value: t, child: Text(t))).toList(),
            onChanged: (value) {
              // BUG 1: setState fehlt – Theme-Änderung wird nicht angezeigt
              _selectedTheme = value ?? 'System';
            },
          ),
          const SizedBox(height: 16),
          // Notifications Toggle
          // BUG 2: SwitchListTile hat keinen korrekten onChanged, der setState aufruft
          // BUG 3: Wird der Dialog geschlossen, müssen die aktuellen Werte an onSave übergeben werden,
          //        aber im onPressed des Save-Buttons wird aktuell hartkodiert "System"/false übergeben
          SwitchListTile(
            title: const Text('Benachrichtigungen'),
            value: _notificationsEnabled,
            onChanged: (value) {
              _notificationsEnabled = value;
            },
            contentPadding: EdgeInsets.zero,
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Abbrechen'),
        ),
        ElevatedButton(
          onPressed: () {
            // BUG 3 (hier): Falsche hartkodierte Werte statt _selectedTheme / _notificationsEnabled
            widget.onSave('System', false);
            Navigator.of(context).pop();
          },
          child: const Text('Speichern'),
        ),
      ],
    );
  }
}
