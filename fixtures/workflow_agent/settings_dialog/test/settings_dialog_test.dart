import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:settings_dialog/settings_dialog.dart';

void main() {
  const total = 8;

  testWidgets('SettingsDialog behaviour', (tester) async {
    var passed = 0;

    Future<void> check(String name, Future<void> Function() body) async {
      try {
        await body();
        passed += 1;
      } catch (error, stackTrace) {
        // ignore: avoid_print
        print('CHECK_FAILED:$name:$error');
        // ignore: avoid_print
        print(stackTrace.toString().split('\n').take(3).join('\n'));
      } finally {
        await tester.pumpAndSettle();
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      }
    }

    Future<void> pumpDialog({
      void Function(String theme, bool notifications)? onSave,
    }) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => ElevatedButton(
              onPressed: () => showDialog(
                context: context,
                builder: (_) => SettingsDialog(
                  onSave: onSave ?? (_, __) {},
                ),
              ),
              child: const Text('Open'),
            ),
          ),
        ),
      ));
      await tester.tap(find.text('Open'));
      await tester.pumpAndSettle();
    }

    await check('dialog shows theme dropdown', () async {
      await pumpDialog();
      expect(find.text('Einstellungen'), findsOneWidget);
      expect(find.text('Theme'), findsOneWidget);
    });

    await check('dialog shows notifications switch', () async {
      await pumpDialog();
      expect(find.text('Benachrichtigungen'), findsOneWidget);
    });

    await check('save button exists', () async {
      await pumpDialog();
      expect(find.text('Speichern'), findsOneWidget);
    });

    await check('cancel button closes dialog', () async {
      await pumpDialog();
      await tester.tap(find.text('Abbrechen'));
      await tester.pumpAndSettle();
      expect(find.text('Einstellungen'), findsNothing);
    });

    await check('save passes selected theme', () async {
      String? savedTheme;
      bool? savedNotifications;
      await pumpDialog(
        onSave: (theme, notifications) {
          savedTheme = theme;
          savedNotifications = notifications;
        },
      );
      // Default theme is 'System', default notifications is true
      await tester.tap(find.text('Speichern'));
      await tester.pumpAndSettle();
      expect(savedTheme, equals('System'));
      expect(savedNotifications, isTrue);
    });

    await check('theme dropdown changes value', () async {
      String? savedTheme;
      await pumpDialog(
        onSave: (theme, _) => savedTheme = theme,
      );
      // Tap dropdown, select 'Dunkel'
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Dunkel').last);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Speichern'));
      await tester.pumpAndSettle();
      expect(savedTheme, equals('Dunkel'));
    });

    await check('notifications toggle changes value', () async {
      bool? savedNotifications;
      await pumpDialog(
        onSave: (_, notifications) => savedNotifications = notifications,
      );
      // Toggle notifications off
      await tester.tap(find.byType(Switch));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Speichern'));
      await tester.pumpAndSettle();
      expect(savedNotifications, isFalse);
    });

    await check('theme and notification combined', () async {
      String? savedTheme;
      bool? savedNotifications;
      await pumpDialog(
        onSave: (theme, notifications) {
          savedTheme = theme;
          savedNotifications = notifications;
        },
      );
      // Change theme to 'Hell'
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Hell').last);
      await tester.pumpAndSettle();
      // Toggle notifications off
      await tester.tap(find.byType(Switch));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Speichern'));
      await tester.pumpAndSettle();
      expect(savedTheme, equals('Hell'));
      expect(savedNotifications, isFalse);
    });

    print('PASSED:$passed/$total');
  });
}
