import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:localized_settings_card/localized_settings_card.dart';

void main() {
  const total = 8;

  testWidgets('LocalizedSettingsCard behaviour', (tester) async {
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

    Future<void> pumpCard({
      void Function(String language, bool notifications)? onSave,
      Map<String, String>? labels,
    }) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: LocalizedSettingsCard(
            onSave: onSave ?? (_, __) {},
            labels: labels,
          ),
        ),
      ));
      await tester.pump();
    }

    // 1. Shows title from labels
    await check('shows title from default labels', () async {
      await pumpCard();
      expect(find.text('Einstellungen'), findsOneWidget);
    });

    // 2. Shows title from custom labels
    await check('shows title from custom labels', () async {
      await pumpCard(labels: {'title': 'Settings', 'save': 'Save', 'cancel': 'Cancel'});
      // BUG: the widget uses hardcoded 'Benachrichtigungen' - this test expects
      // that at minimum the title comes from labels
      expect(find.text('Settings'), findsOneWidget);
    });

    // 3. Save button passes current values
    await check('save passes current language and notification defaults', () async {
      String? savedLang;
      bool? savedNotif;
      await pumpCard(onSave: (lang, notif) {
        savedLang = lang;
        savedNotif = notif;
      });
      await tester.tap(find.text('Speichern'));
      await tester.pumpAndSettle();
      // Default: Deutsch, notifications true
      expect(savedLang, equals('Deutsch'));
      expect(savedNotif, isTrue);
    });

    // 4. Language dropdown changes value
    await check('language dropdown changes value', () async {
      String? savedLang;
      await pumpCard(onSave: (lang, _) => savedLang = lang);
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Englisch').last);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Speichern'));
      await tester.pumpAndSettle();
      expect(savedLang, equals('Englisch'));
    });

    // 5. Notifications toggle changes value
    await check('notifications toggle changes value', () async {
      bool? savedNotif;
      await pumpCard(onSave: (_, notif) => savedNotif = notif);
      await tester.tap(find.byType(Switch));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Speichern'));
      await tester.pumpAndSettle();
      expect(savedNotif, isFalse);
    });

    // 6. Cancel button exists
    await check('cancel button exists', () async {
      await pumpCard();
      expect(find.text('Abbrechen'), findsOneWidget);
    });

    // 7. Long text is visible (doesn't crash/overflow silently)
    await check('long description text is visible', () async {
      await pumpCard();
      expect(find.textContaining('Diese Einstellungen'), findsOneWidget);
    });

    // 8. Combined language + notification change
    await check('combined language and notification change', () async {
      String? savedLang;
      bool? savedNotif;
      await pumpCard(onSave: (lang, notif) {
        savedLang = lang;
        savedNotif = notif;
      });
      // Change language to Französisch
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Französisch').last);
      await tester.pumpAndSettle();
      // Toggle notifications off
      await tester.tap(find.byType(Switch));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Speichern'));
      await tester.pumpAndSettle();
      expect(savedLang, equals('Französisch'));
      expect(savedNotif, isFalse);
    });

    print('PASSED:$passed/$total');
  });
}
