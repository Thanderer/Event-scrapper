**TODO
{
  "source": "website_name",
  "source_url": "https://...",
  "scraped_at": "2026-06-08T16:00:00Z",
  "hash_id": "a1b2c3d4e5f6",
  "name": "Event Title",
  "description": "Long text...",
  "category": "Music / Sports", //marge this with keywards
  "keywords": ["tag1", "tag2"],
  
  "start_iso": "2026-06-12T18:00:00",
  "end_iso": "2026-06-12T20:00:00",
  
  "venue_name": "Elbauenpark",
  "address": "Tessenowstraße 7, 39114 Magdeburg",
  "geo_lat": 52.1384,
  "geo_lon": 11.6662,
  
  "price": "Moderate",
  
  "image_url": "https://...",
  "image_local": "data/images/...",
  "is_series": true, //remove this
  "series_id": ["hash1", "hash2"], //is it is not series yet than have null as value
  "repeat_frequency": "[yearly]"
}
"""info: this project script will run everyday because we need to include demonstration and server are restarting/ updating every night"""
 
#TODO: create python a project with postgrasesql and docker (oops concept) for scrapper 
- create docker compose.yaml and dockerfile
- we want to have only one DB (the schema is already above DO NOT OVERENGINEER THIS) also do not worry about normal forms and handle the image storage
- the DB should not be affected by state of scrapper project
- scrapper must be stateless
- for that implement some list, dataclasses, abstract method, etc..
	interface create an scraper interface-> separate for each scraper (also separate file)
		scrape function that takes list of all the scrapper gets the latest event (this will create cold start problem and solution is if query result for latest_event from db is null or result does not exist than scrape till 1 year from now)
		min time for scrapping(min(latest_event, datetime.now + oneweek); this will confuse you that with this you will only scrape 1 week days data, it is ment to, because you(jash) need to comeup with something that can give you best time that you can also incoorporate any updates that are made to the event alongside have tentative events till next year)
		than marge of all the events (handle missing data, duplicates across events, and recurrent events(add reference to the recurrent events)
		finally replace all the old events from that timeframe from db and insert new one