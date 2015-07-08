from __future__ import division
import os
import subprocess
import json
from flask import Flask
from flask import jsonify
from flask import session, url_for, flash
from werkzeug.http import parse_options_header
from flask import Flask, request, make_response, redirect
from flask.templating import render_template
from flask import Response


import collections
import re
# encoding=utf8

class Tag:    
    def __init__(self, string, stem=None, rating=1.0, proper=False,
                 terminal=False):
        self.string  = string
        self.stem = stem or string
        self.rating = rating
        self.proper = proper
        self.terminal = terminal
        
    def __eq__(self, other):
        return self.stem == other.stem

    def __repr__(self):
        return repr(self.string)

    def __lt__(self, other):
        return self.rating > other.rating

    def __hash__(self):
        return hash(self.stem)


class MultiTag(Tag):
    def __init__(self, tail, head=None):
        if not head:
            Tag.__init__(self, tail.string, tail.stem, tail.rating,
                         tail.proper, tail.terminal)
            self.size = 1
            self.subratings = [self.rating]
        else:
            self.string = ' '.join([head.string, tail.string])
            self.stem = ' '.join([head.stem, tail.stem])
            self.size = head.size + 1

            self.proper = (head.proper and tail.proper)
            self.terminal = tail.terminal

            self.subratings = head.subratings + [tail.rating]
            self.rating = self.combined_rating()
                                           
    def combined_rating(self):
        product = reduce(lambda x, y: x * y, self.subratings, 1.0)
        root = self.size
        
        # but proper nouns shouldn't be penalized by stopwords
        if product == 0.0 and self.proper:
            nonzero = [r for r in self.subratings if r > 0.0]
            if len(nonzero) == 0:
                return 0.0
            product = reduce(lambda x, y: x * y, nonzero, 1.0)
            root = len(nonzero)
            
        return product ** (1.0 / root)

    
class Reader:
    match_apostrophes = re.compile(r'`|\xe2')
    match_paragraphs = re.compile(r'[\.\?!\t\n\r\f\v]+')
    match_phrases = re.compile(r'[,;:\(\)\[\]\{\}<>]+')
    match_words = re.compile(r'[\w\-\'_/&]+')
    
    def __call__(self, text):
        text = self.preprocess(text)

        # split by full stops, newlines, question marks...
        paragraphs = self.match_paragraphs.split(text)

        tags = []

        for par in paragraphs:
            # split by commas, colons, parentheses...
            phrases = self.match_phrases.split(par)

            if len(phrases) > 0:
                # first phrase of a paragraph
                words = self.match_words.findall(phrases[0])
                if len(words) > 1:
                    tags.append(Tag(words[0].lower()))
                    for w in words[1:-1]:
                        tags.append(Tag(w.lower(), proper=w[0].isupper()))
                    tags.append(Tag(words[-1].lower(),
                                    proper=words[-1][0].isupper(),
                                    terminal=True))
                elif len(words) == 1:
                    tags.append(Tag(words[0].lower(), terminal=True))

            # following phrases
            for phr in phrases[1:]:
                words = self.match_words.findall(phr)
                if len(words) > 1:
                    for w in words[:-1]:
                        tags.append(Tag(w.lower(), proper=w[0].isupper()))
                if len(words) > 0:
                    tags.append(Tag(words[-1].lower(),
                                    proper=words[-1][0].isupper(),
                                    terminal=True))

        return tags

    def preprocess(self, text):
        text = self.match_apostrophes.sub('\'', text)
        return text

    
class Stemmer:
    match_contractions = re.compile(r'(\w+)\'(m|re|d|ve|s|ll|t)?')
    match_hyphens = re.compile(r'\b[\-_]\b')

    def __init__(self, stemmer=None):
        if not stemmer:
            from stemming import porter2
            stemmer = porter2
        self.stemmer = stemmer

    def __call__(self, tag):
        string = self.preprocess(tag.string)
        tag.stem = self.stemmer.stem(string)
        return tag    
        
    def preprocess(self, string):
        # delete hyphens and underscores
        string = self.match_hyphens.sub('', string)
        
        # get rid of contractions and possessive forms
        match = self.match_contractions.match(string)
        if match: string = match.group(1)
        
        return string
    

class Rater:
      def __init__(self, weights, multitag_size=3):        
        self.weights = weights
        self.multitag_size = multitag_size
        
      def __call__(self, tags):
        self.rate_tags(tags)
        multitags = self.create_multitags(tags)

        # keep most frequent version of each tag
        clusters = collections.defaultdict(collections.Counter)
        proper = collections.defaultdict(int)
        ratings = collections.defaultdict(float)
        
        for t in multitags:
            clusters[t][t.string] += 1
            if t.proper:
                proper[t] += 1
                ratings[t] = max(ratings[t], t.rating)

        term_count = collections.Counter(multitags)
                
        for t, cnt in term_count.iteritems():
            t.string = clusters[t].most_common(1)[0][0]
            proper_freq = proper[t] / cnt
            if proper_freq >= 0.5:
                t.proper = True
                t.rating = ratings[t]
        
        # purge duplicates, one-character tags and stopwords
        unique_tags = set(t for t in term_count
                          if len(t.string) > 1 and t.rating > 0.0)
        # remove redundant tags
        for t, cnt in term_count.iteritems():
            words = t.stem.split()
            for l in xrange(1, len(words)):
                for i in xrange(len(words) - l + 1):
                    s = Tag(' '.join(words[i:i + l]))
                    relative_freq = cnt / term_count[s]
                    if ((relative_freq == 1.0 and t.proper) or
                        (relative_freq >= 0.5 and t.rating > 0.0)):
                        unique_tags.discard(s)
                    else:
                        unique_tags.discard(t)
        
        return sorted(unique_tags)

      def rate_tags(self, tags):
        '''
        @param tags: a list of tags to be assigned a rating
        '''
        
        term_count = collections.Counter(tags)
        
        for t in tags:
            # rating of a single tag is term frequency * weight
            t.rating = term_count[t] / len(tags) * self.weights.get(t.stem, 1.0)
    
      def create_multitags(self, tags):
        '''
        @param tags: a list of tags (respecting the order in the text)

        @returns: a list of multitags
        '''
        
        multitags = []
        
        for i in xrange(len(tags)):
            t = MultiTag(tags[i])
            multitags.append(t)
            for j in xrange(1, self.multitag_size):
                if t.terminal or i + j >= len(tags):
                    break
                else:
                    t = MultiTag(tags[i + j], t)
                    multitags.append(t)

        return multitags
    
    
class Tagger:
    '''
    Master class for tagging text documents

    (this is a simple interface that should allow convenient experimentation
    by using different classes as building blocks)
    '''

    def __init__(self, reader, stemmer, rater):
        '''
        @param reader: a L{Reader} object
        @param stemmer: a L{Stemmer} object
        @param rater: a L{Rater} object

        @returns: a new L{Tagger} object
        '''
        
        self.reader = reader
        self.stemmer = stemmer
        self.rater = rater

    def __call__(self, text, tags_number=6):
        '''
        @param text:        the string of text to be tagged
        @param tags_number: number of best tags to be returned

        Returns: a list of (hopefully) relevant tags
        ''' 
        tags = self.reader(text)
        tags = map(self.stemmer, tags)
        tags = self.rater(tags)

        return tags[:tags_number]

class Object:
    def to_JSON(self):
        return json.dumps(self, default=lambda o: o.__dict__,sort_keys=False, indent=3)


def mytagger(text,numtags):
    import globgit
    import pickle
    import sys
    numtags = 6
    leng = len(text)
    
        
       
        
    weights = pickle.load(open('data/dict.pkl', 'rb'))

    tagger = Tagger(Reader(), Stemmer(), Rater(weights))
    generatedtags = tagger(text,numtags)
    return generatedtags

app = Flask(__name__)
@app.route('/')
def hello():
    return render_template('index.html')

@app.route('/tagger', methods=['GET', 'POST'])
def taggerworks():
    if request.form:
        document = str(request.form['document'])
        passcode = str(request.form['passcode'])
        documentID = str(request.form['documentid'])
        numtags = request.form['numtags']
        if passcode != "bangalore":
            return "Invalid Passcode"
        #output = subprocess.check_output("./tagger.py "+document, shell=True)
        output = mytagger(document)
        list = []
        print output
        doc = Object()
        doc.footprint = "Tathagat Nawadia"
        doc.original_text = document
        doc.documentID = documentID
        doc.tags  = output
        h = doc.to_JSON()
        return Response(h,  mimetype='application/json')
    else:
        return "BAD REQUEST .. SOME PEOPLE JUST WANT THE WORLD TO BURN"

app.run(host=os.environ['IP'],port=int(os.environ['PORT']))