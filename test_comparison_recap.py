import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import comparison_recap as recap
import sync_description as sync
from test_route_matches import activity

class ComparisonTests(unittest.TestCase):
    def test_effort_fields_are_separate_and_missing_stays_unknown(self):
        f=recap.activity_facts({'suffer_score': 58, 'perceived_exertion': 6})
        self.assertEqual(f['relative_effort'],58)
        self.assertEqual(f['reported_effort_1_to_10'],6)
        for bad in (None, 0, 11, True, float('nan')):
            self.assertIsNone(recap.activity_facts({'perceived_exertion':bad})['reported_effort_1_to_10'])
        self.assertIsNone(recap.activity_facts({'suffer_score':58})['reported_effort_1_to_10'])
        self.assertEqual(recap.activity_facts({'suffer_score':0})['relative_effort'],0)

    def test_allowlist_and_differences(self):
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        weather={'hourly':{'time':[int(start.timestamp())], 'temperature_2m':[18]}}
        old={'hourly':{'time':[int(start.timestamp())], 'temperature_2m':[25]}}
        current={'distance':5000,'moving_time':1750,'suffer_score':30,'id':123,'name':'SECRETNAME','description':'SECRETNOTE','start_latlng':[37,-122]}
        previous={'distance':5000,'moving_time':1800,'suffer_score':40}
        facts=recap.build_comparison_input(current,weather,start,'url',previous,old,start,'url')
        self.assertEqual(facts['differences_current_minus_comparison']['weather']['temperature_c'],-7)
        self.assertEqual(facts['differences_current_minus_comparison']['activity']['average_moving_pace_seconds_per_km'],-10)
        self.assertIsNone(facts['current']['activity']['reported_effort_1_to_10'])
        for private in ('SECRETNAME','SECRETNOTE','start_latlng','"id"'):
            self.assertNotIn(private,json.dumps(facts))
        no_weather=recap.build_comparison_input(current,weather,start,'url',previous)
        self.assertIsNone(no_weather['comparison']['weather'])
        self.assertEqual(no_weather['differences_current_minus_comparison']['weather'],{})

    def test_invalid_marker_output_rejected(self):
        with patch.object(recap,'generate_recap',return_value={'recap':'[Workout Weather Recap]','caveats':['x']}):
            with self.assertRaises(RuntimeError): recap.narrate({},key='fake')

    def test_relative_percentage_claim_rejected(self):
        with patch.object(recap,'generate_recap',return_value={'recap':'Humidity was 19% higher.','caveats':['Estimated']}):
            with self.assertRaises(RuntimeError): recap.narrate({},key='fake')
        with patch.object(recap,'generate_recap',return_value={'recap':'Humidity was 19 percentage points higher.','caveats':['Estimated']}):
            text, _ = recap.narrate({},key='fake')
            self.assertIn('percentage points',text)

    def test_no_match_success_and_fallback_preserve_notes(self):
        target=activity(name='Run',athlete={'id':7},description='My notes',suffer_score=58)
        start=datetime.fromisoformat(target['start_date'])
        weather={'hourly':{'time':[int(start.timestamp())],'temperature_2m':[18]}}
        report={'reason':'No comparable similar-route run found in the previous 90 days','selected_id':None}
        with patch.object(sync,'fetch_json',return_value=weather), \
             patch.object(sync,'weather_request',return_value=('url',start)), \
             patch.object(sync,'select_match',return_value=(report,None)), \
             patch.object(sync,'narrate',return_value=('Generated weather recap',{'recap':'Generated weather recap','caveats':['Estimated']})) as narrate:
            draft=sync.prepare_draft(1,token='fake',activity=target,use_gemini=True,gemini_key='fake')
            self.assertIsNone(narrate.call_args.args[0]['comparison'])
            self.assertEqual(draft['generation']['mode'],'gemini')
            self.assertTrue(draft['proposed_description'].startswith('My notes'))
            self.assertIn('Generated weather recap',draft['proposed_description'])
            narrate.side_effect=RuntimeError('secret error detail')
            draft=sync.prepare_draft(1,token='fake',activity=target,use_gemini=True,gemini_key='fake')
            self.assertEqual(draft['generation']['mode'],'template_fallback')
            self.assertIn('18°C',draft['proposed_description'])
            self.assertNotIn('secret error detail',json.dumps(draft))

    def test_comparison_fetches_detail_for_effort(self):
        target=activity(name='Run',athlete={'id':7})
        old=activity(2,2,name='Older',athlete={'id':7},perceived_exertion=5,suffer_score=60)
        start=datetime.fromisoformat(target['start_date'])
        weather={'hourly':{'time':[int(start.timestamp())],'temperature_2m':[18]}}
        report={'reason':'Most recent suitable similar-route run','selected_id':2}
        with patch.object(sync,'fetch_json',side_effect=[weather,old,weather]) as fetch, \
             patch.object(sync,'weather_request',return_value=('url',start)), \
             patch.object(sync,'select_match',return_value=(report,{'id':2})), \
             patch.object(sync,'narrate',return_value=('recap',{'recap':'recap','caveats':['Estimated']})) as narrate:
            sync.prepare_draft(1,token='fake',activity=target,use_gemini=True,gemini_key='fake')
            self.assertEqual(fetch.call_args_list[1].args[0],'https://www.strava.com/api/v3/activities/2')
            self.assertEqual(narrate.call_args.args[0]['comparison']['activity']['reported_effort_1_to_10'],5)

if __name__=='__main__': unittest.main()
